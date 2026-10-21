"""Bounded task-level projections for local assistant adapters."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from shared.chatgpt_auth import chatgpt_status
from shared.model_catalog import build_model_catalog, is_chatgpt_subscription_model
from shared.schemas import TaskStatus
from shared.steering import SteeringInbox, SteeringRequest
from shared.assistant_presentation import assistant_onboarding, task_page_presentation
from shared.hitl import HumanInputBroker, InputConflict

TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
CONSOLE_HELP = "向用户提供 console_url：观测截图查看已保存的任务画面，Timeline 查看步骤和模型输入输出，Trace 查看详细日志。快照是已保存的历史画面。"
DEVICE_SETUP = {
    "physical_phone": "Android 8.0+/API 26：可用 USB 调试/数据线并接受电脑授权；Android 11+ 也可同一 Wi-Fi 开启无线调试，通过配对码配对后连接，不需要数据线。配对端口和连接端口不同；adb devices -l 中应为 device，unauthorized/offline 均未就绪。",
    "emulator": "启动支持 ADB 的 Android 8.0+/API 26 模拟器并等待开机；先用 adb devices -l 查实际序列号。需网络连接时按模拟器文档使用实际 ADB 主机/端口，不猜测固定端口。",
    "local_driver": "设备接在这台 PC 时，.env 设置 CLICKCLICK_USE_FIXTURE_DRIVER=false，清空 CLICKCLICK_DRIVER_URL 与此前配置的 CLICKCLICK_DRIVER_URLS_JSON，然后重启后台。本地使用内嵌 Driver。",
    "collector": "后台自动初始化在线空闲设备，安装/升级 Collector 并启用无障碍；设备出现权限或受限设置提示时按 guidance 操作。环境步骤由 get_status 返回，手动重试接口见部署指南；Console Devices 查看在线/忙碌状态。",
    "input_method": "设备已安装 ADBKeyboard 可直接使用；否则自行准备 APK，在 .env 设置 CLICKCLICK_IME_APK_PATH 后重启。平台安装该文件但不自动下载；检查 environment.steps.test_ime。",
}


class AssistantTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=20000)
    request_key: str = Field(min_length=1, max_length=160)
    device_ids: list[str] | None = Field(default=None, min_length=1, max_length=32)
    reply_binding: str | None = Field(default=None, min_length=32, max_length=128)
    submission_origin: str = Field(default="assistant", pattern="^(assistant|cli)$")
    hitl_enabled: bool | None = None


def mount_assistant_api(app, *, db, artifacts, pool, settings, create_task, task_body,
                        submission_lock):
    router = APIRouter(prefix="/api/assistant")
    host = settings.api_host if settings.api_host not in {"0.0.0.0", "::"} else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "https" if settings.api_ssl_certfile and settings.api_ssl_keyfile else "http"
    base_url = f"{scheme}://{host}:{settings.api_port}"

    def links(task_id=None):
        return {"console_url": base_url + (f"/tasks/{quote(task_id, safe='')}" if task_id else "/"),
                "console_help": CONSOLE_HELP}

    def compact(task):
        state = task.state
        runtime = state.revisable if state else None
        result = runtime.completion_reason if runtime else ""
        last = db.latest_agent_record(task.id, "event")
        progress = (last["payload"].get("executor_report", "") if last else "")
        payload = {
            "task_id": task.id, "status": task.status.value,
            "instruction": task.instruction[:2000], "device_id": task.device_serial,
            "created_at": task.created_at, "updated_at": task.updated_at,
            "elapsed_seconds": max(0, (task.updated_at if task.status in TERMINAL else time.time()) - task.created_at),
            "current_stage": runtime.stage.goal[:2000] if runtime and runtime.stage else None,
            "next_role": runtime.next_role if runtime else None,
            "step_number": task.step_number,
            "model_requests": state.role_invocation_count if state else None,
            "device_actions": runtime.execution_count if runtime else None,
            "deadline_at": runtime.limits.deadline_at if runtime else None,
            "progress": str(progress)[:2000], "result": result[:6000] or None,
            "result_truncated": len(result) > 6000,
            "failure_reason": (task.failure_reason or "")[:2000] or None,
            **links(task.id),
        }
        from shared.hitl import HumanInputBroker
        broker = HumanInputBroker(db)
        if broker.config(task.id)["enabled"]:
            payload["human_input"] = broker.public(task.id)
            changes = SteeringInbox(db, task.id).projection()
            payload["adjustment"] = changes[-1] if changes else None
        if runtime:
            from shared.task_budgets import resumable_budget_failure, execution_attempts
            payload["stop_reason"] = runtime.stop_reason
            payload["budget_kind"] = runtime.budget_kind
            payload["budget_limits"] = runtime.limits.model_dump()
            payload["can_resume"] = task.status == TaskStatus.PAUSED or resumable_budget_failure(task)
            payload["execution_attempt"] = runtime.execution_attempt
            payload["execution_attempts"] = execution_attempts(db, task.id)
        # Enrich only after the original payload exists. Preview import,
        # calculation and serialization errors cannot fail an accepted task.
        preview = {"preview_url": None, "preview_support": {
            "status": "unavailable", "reason": "preview_metadata_error",
        }}
        try:
            from control_api.preview import safe_preview_projection
            preview = safe_preview_projection(
                task, settings=settings, pool=pool, base_url=base_url,
                enabled=app.state.task_preview is not None,
            )
        except Exception:
            pass
        payload.update(preview)
        return payload

    def model_readiness():
        catalog = build_model_catalog(settings)
        providers = settings.model_providers()
        roles = {role: catalog["roles"][role] for role in ("planner", "reviewer", "executor")}
        missing = [role for role, model in roles.items() if model not in providers]
        subscription_required = any(
            model in providers and is_chatgpt_subscription_model(model, providers[model])
            for model in roles.values())
        subscription_authenticated = (chatgpt_status(settings)["authenticated"]
                                      if subscription_required else None)
        return roles, missing, {
            "subscription_required": subscription_required,
            "subscription_authenticated": subscription_authenticated,
        }

    @router.get("/identity")
    async def identity():
        return {"service": "clickclick", "assistant_api_version": 1,
                "data_dir": str(settings.data_dir.resolve()),
                "workspace": str(Path.cwd().resolve()), "pid": os.getpid()}

    @router.get("/status")
    async def status():
        inventory = await pool.inventory()
        busy = db.busy_serials()
        devices = [{"device_id": d.get("key") or d["serial"],
                    "name": d.get("market_name") or d.get("model") or d["serial"],
                    "busy_task_id": busy.get(d.get("key") or d["serial"]),
                    "environment": pool.environment_status(d.get("key") or d["serial"])}
                   for d in inventory[:64]]
        roles, missing, authentication = model_readiness()
        guidance = []
        if missing:
            guidance.append("在 ClickClick Console 设置页选择 API 或订阅通道并配置各角色模型。")
        if authentication["subscription_authenticated"] is False:
            guidance.append("在 ClickClick Console 设置页完成 ChatGPT 订阅登录授权。MCP 使用 ClickClick 自己的模型授权，不会自动复用助手登录。")
        if not devices:
            guidance.append("没有在线设备：真机检查 USB 调试/数据线/ADB 授权，或同一 Wi-Fi 的无线调试配对与实际连接端口；模拟器检查开机与 ADB 连接。按 device_setup 排查，adb devices -l 应显示 device。")
        for device in devices:
            environment = device["environment"]
            if not environment:
                guidance.append(f"设备 {device['device_id']} 尚无初始化结果：等待后台初始化后再次查询 get_status，不要仅凭在线就报告完全就绪。Console Devices 可查看在线/忙碌状态。")
            elif environment.get("status") != "ready":
                guidance.append(f"设备 {device['device_id']} 环境状态为 {environment.get('status', 'unknown')}：查看返回的 environment.steps 与 guidance，按部署指南处理后等待后台重试。")
        docs_dir = Path.cwd() / "docs"
        setup_guides = {name: str((docs_dir / filename).resolve()) for name, filename in {
            "desktop_setup": "desktop-setup.zh-CN.md",
            "assistant_setup": "local-mcp.zh-CN.md",
            "deployment": "deployment.zh-CN.md",
            "collector": "accessibility-collector-setup.zh-CN.md",
        }.items() if (docs_dir / filename).is_file()}
        return {"backend": await identity(), "devices": devices, "hitl_enabled": settings.hitl_enabled,
                "assistant_onboarding": assistant_onboarding(hitl_enabled=settings.hitl_enabled, setup_url=base_url + "/setup"),
                "devices_truncated": len(inventory) > 64,
                "models_configured": not missing, "roles": roles,
                "missing_model_roles": missing, "model_authentication": authentication,
                "credentials_validated": False,
                "guidance": guidance, "device_setup": DEVICE_SETUP, "setup_guides": setup_guides,
                "devices_url": base_url + "/device", "setup_url": base_url + "/setup", **links()}

    async def _start(body: AssistantTaskBody, background_tasks: BackgroundTasks):
        # Idempotent replay must work even when the device later goes offline.
        if not db.get_submission(body.request_key):
            _, missing, authentication = model_readiness()
            if missing:
                raise HTTPException(409, "Model configuration required; call get_status for setup guidance")
            if authentication["subscription_authenticated"] is False:
                raise HTTPException(409, "ClickClick subscription login required; call get_status for setup guidance")
        devices = body.device_ids
        submission = db.get_submission(body.request_key)
        if devices is None and submission:
            devices = [db.get_task(tid).device_serial for tid in submission["task_ids"]]
        if devices is None:
            inventory = await pool.inventory()
            available = [d.get("key") or d["serial"] for d in inventory
                         if (d.get("key") or d["serial"]) not in db.busy_serials()]
            if len(available) != 1:
                raise HTTPException(409, "select device_ids explicitly using get_status")
            devices = available
        result = await create_task(task_body(instruction=body.instruction,
                                            device_serials=devices, request_key=body.request_key,
                                            hitl_enabled=body.hitl_enabled),
                                   background_tasks, origin=body.submission_origin, binding=body.reply_binding)
        tasks = [compact(db.get_task(t["id"])) for t in result["tasks"]]
        return {"tasks": tasks, "assistant_ui": task_page_presentation(tasks),
                "replayed": result.get("replayed", False)}

    @router.post("/tasks")
    async def start(body: AssistantTaskBody, background_tasks: BackgroundTasks):
        # Include implicit-device selection in the same lock as dedup/create.
        async with submission_lock:
            return await _start(body, background_tasks)

    @router.post("/tasks/{task_id}/steer")
    async def steer(task_id: str, body: SteeringRequest, background_tasks: BackgroundTasks):
        async with submission_lock:
            task = db.get_task(task_id)
            if task is None:
                raise HTTPException(404, "task not found")
            config = HumanInputBroker(db).config(task_id)
            if not config["enabled"]:
                return {"task_id": task_id, "status": "disabled", "code": "hitl_disabled",
                        "message": "请在 Console 设置中打开 HITL 开关后创建新任务，才可中途干预。已有任务按创建时配置运行；如活动任务阻止保存设置，请先等待其结束或取消。",
                        "settings_url": base_url + "/setup", **links(task_id)}
            inbox = SteeringInbox(db, task_id)
            try:
                previous = inbox.previous(body)
            except InputConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            if previous:
                return {"task_id": task_id, **previous["payload"], "replayed": True, **links(task_id)}
            if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} or app.state.orchestrator.is_cancel_requested(task_id):
                raise HTTPException(409, "task failed or cancelled; inspect its result before starting new work")
            live = app.state.running_tasks.get(task_id)
            if task.status == TaskStatus.RUNNING and (live is None or live.done()):
                raise HTTPException(409, "orphan running task; inspect or cancel it before continuing")
            from desktop.updates import require_admission
            require_admission(settings.data_dir)
            if task.status == TaskStatus.SUCCEEDED:
                import hashlib
                followup_key = "steer:" + hashlib.sha256((task_id + ":" + body.request_key).encode()).hexdigest()
                intent = db.get_agent_record(task_id, "followup_intent", body.request_key)
                if intent and intent["payload"]["adjustment"] != body.instruction:
                    raise HTTPException(409, "request_key_conflict")
                if intent is None:
                    from agent.revisable.recall import human_evidence_record
                    answers = [human_evidence_record(r)["payload"] for r in db.list_agent_records(task_id, "human_input")
                               if r["payload"]["status"] in {"resolved", "consumed"}]
                    result = task.state.revisable.completion_reason if task.state else ""
                    instruction = ("Follow-up to completed task " + task_id + ". Actual prior effects are not undone.\n"
                                   "Original request:\n" + task.instruction + "\nPrior adjustments:\n" +
                                   json.dumps([r["instruction"] for r in inbox.projection()], ensure_ascii=False) +
                                   "\nPrior user answers:\n" + json.dumps(answers, ensure_ascii=False) +
                                   "\nPrior result:\n" + result + "\nNew user request:\n" + body.instruction)
                    db.append_agent_record(task_id, "followup_intent", body.request_key,
                                           {"adjustment": body.instruction, "instruction": instruction})
                else:
                    instruction = intent["payload"]["instruction"]
                created = await create_task(task_body(instruction=instruction, device_serials=[task.device_serial],
                                                      request_key=followup_key, hitl_enabled=True),
                                            background_tasks, origin=config.get("origin", "assistant"), binding=config.get("binding"))
                child_id = created["tasks"][0]["id"]
                with db.transaction():
                    db.append_agent_record(child_id, "parent_task", "source", {"task_id": task_id})
                    item = inbox.accept(body, status="follow_up", followup_task_id=child_id)
                return {"task_id": task_id, **item, "followup_console_url": links(child_id)["console_url"],
                        "assistant_ui": task_page_presentation([compact(db.get_task(child_id))]), **links(task_id)}
            with db.transaction():
                question = inbox.supersede_question()
                item = inbox.accept(body, **({"superseded_question": question} if question else {}))
                if task.status == TaskStatus.WAITING_FOR_INPUT:
                    db.update_task(task_id, status=TaskStatus.QUEUED)
            control = app.state.orchestrator.steering_controls.get(task_id)
            if control is not None:
                control.notify()
            if task.status in {TaskStatus.QUEUED, TaskStatus.WAITING_FOR_INPUT}:
                background_tasks.add_task(app.state.schedule_task, task_id)
            return {"task_id": task_id, **item, "instruction_revision": item["revision"], **links(task_id)}

    @router.get("/tasks")
    async def listing(status: TaskStatus | None = None,
                      limit: int = Query(default=10, ge=1, le=50),
                      cursor: str | None = Query(default=None, max_length=512)):
        before = None
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded["status"] != (status.value if status else None):
                    raise ValueError("cursor status mismatch")
                before = (float(decoded["created_at"]), str(decoded["task_id"]))
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise HTTPException(400, "invalid cursor") from exc
        tasks = db.assistant_task_page(status=status, limit=limit + 1, before=before)
        selected = tasks[:limit]
        next_cursor = None
        if len(tasks) > limit:
            tail = selected[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "created_at": tail.created_at, "task_id": tail.id,
                "status": status.value if status else None,
            }).encode()).decode()
        return {"tasks": [compact(t) for t in selected], "next_cursor": next_cursor, **links()}

    @router.get("/tasks/{task_id}")
    async def get(task_id: str, wait_seconds: float = Query(default=0, ge=0, le=30)):
        task = db.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        initial = task.updated_at
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while task.status not in TERMINAL | {TaskStatus.PAUSED, TaskStatus.WAITING_FOR_INPUT} and task.updated_at == initial:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(.25, remaining))
            task = db.get_task(task_id)
        return compact(task)

    @router.get("/tasks/{task_id}/snapshot")
    async def snapshot(task_id: str):
        if db.get_task(task_id) is None:
            raise HTTPException(404, "task not found")
        record = db.latest_agent_record(task_id, "observation", image_only=True)
        result = {"task_id": task_id, "image_available": False, "historical": True, **links(task_id)}
        if record:
            payload = record["payload"]
            ref = payload["image_ref"]
            path = artifacts.resolve(ref)
            if path.is_file():
                result.update(image_available=True, observation_id=record["key"],
                              step=payload.get("step"), recorded_at=payload.get("recorded_at"),
                              image_url=f"{base_url}/api/tasks/{quote(task_id, safe='')}/artifacts/{quote(ref, safe='/')}")
        return result

    app.include_router(router)
