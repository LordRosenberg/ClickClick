"""Authenticated local onboarding, outside the assistant's task MCP tools."""

import asyncio
from pathlib import Path
import os
from contextlib import asynccontextmanager
import sys
import ssl
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from starlette.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control_api.mcp_http import LocalBoundary, local_token, http_base_url
from desktop.configuration import model_summary, save_model
from desktop import devices
from desktop.registration import server_entry
from desktop.connection import connection_info


class ModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: SecretStr


class PairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=80)
    code: SecretStr


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=80)


class ConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: Literal["zh-CN", "en"] = "zh-CN"


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    confirmed: bool = False


class UpdatePreferenceBody(BaseModel):
    auto_check: bool


class UpdateJobBody(BaseModel):
    job: str = Field(pattern=r"^[a-f0-9]{32}$")


class UpdateRecoveryBody(BaseModel):
    confirmed: bool = False


def mount_setup_api(app, *, settings, submission_lock):
    router = APIRouter(prefix="/api/setup")
    host = ("127.0.0.1" if settings.api_host in {"0.0.0.0", "::"} else
            "[::1]" if settings.api_host == "::1" else settings.api_host)
    scheme = "https" if settings.api_ssl_certfile and settings.api_ssl_keyfile else "http"
    app.add_middleware(LocalBoundary, port=settings.api_port, scheme=scheme,
                       token=local_token(settings), setup_only=not settings.mcp_http_enabled)
    base_url = f"{scheme}://{host}:{settings.api_port}"
    install_home = os.environ.get("CLICKCLICK_INSTALL_HOME")
    from desktop import updates
    from control_api.console_settings import CoreSettingsBody, prepare_settings, persist_settings, settings_summary
    from shared import chatgpt_auth
    pending_login = None
    login_lock = asyncio.Lock()

    @router.get("/access")
    async def browser_access():
        return JSONResponse({"token": local_token(settings)}, headers={"Cache-Control": "no-store"})

    @router.get("/settings")
    async def core_settings():
        return JSONResponse(settings_summary(settings), headers={"Cache-Control": "no-store"})

    @router.post("/settings")
    async def save_core_settings(body: CoreSettingsBody):
        async with submission_lock:
            has_tasks = any(task["status"] in {"queued", "running", "pausing", "paused"}
                            for task in app.state.db.task_headers())
            if has_tasks or app.state.running_tasks or app.state.orchestrator._learning_jobs:
                raise HTTPException(409, "请先完成或取消未结束的任务（包括暂停任务）及个人优化，再保存模型与运行设置。")
            async with login_lock:
                if pending_login is not None:
                    raise HTTPException(409, "订阅授权进行中，请先完成或停止授权。")
                try:
                    document, checked = prepare_settings(settings, body)
                    persist_settings(settings, document, checked)
                except ValueError:
                    # Never echo provider body, key or input from nested validation.
                    raise HTTPException(400, "配置无效：请检查模型、角色、API Key、地址及额外 JSON 参数。") from None
                except OSError:
                    raise HTTPException(503, "无法保存本机设置，请检查数据目录权限。") from None
        return {"saved": True, "restart_required": False, "credentials_validated": False,
                "guidance": "设置已保存，新任务立即使用；保存不会发起模型请求。"}

    @router.get("/chatgpt/status")
    async def subscription_status():
        return chatgpt_auth.chatgpt_status(settings)

    @router.post("/chatgpt/login/start")
    async def start_subscription_login():
        nonlocal pending_login
        async with login_lock:
            if pending_login is not None:
                raise HTTPException(409, "已有授权进行中，请先完成或停止授权。")
            try:
                payload, pending_login = await asyncio.to_thread(chatgpt_auth.start_device_login, settings)
                return payload
            except Exception:
                raise HTTPException(502, "无法发起订阅授权，请检查网络后重试。") from None

    @router.post("/chatgpt/login/poll")
    async def poll_subscription_login():
        nonlocal pending_login
        async with login_lock:
            if pending_login is None:
                raise HTTPException(409, "暂无进行中的授权，请重新登录。")
            try:
                _, payload, pending_login = await asyncio.to_thread(chatgpt_auth.poll_device_login, settings, pending_login)
                if payload.get("status") == "error":
                    return {"status": "error", "error": "授权未完成或已超时，请重新登录。"}
                return payload
            except Exception:
                pending_login = None
                raise HTTPException(502, "授权未完成，请重新登录。") from None

    @router.post("/chatgpt/login/cancel")
    async def cancel_subscription_login():
        nonlocal pending_login
        async with login_lock:
            pending_login = None
        return {"cancelled": True}

    def installed_home():
        if not install_home:
            raise HTTPException(409, "自动更新仅适用于桌面安装版本；源码部署请使用自己的代码更新流程。")
        return Path(install_home).resolve()

    @router.post("/shutdown/prepare")
    async def prepare_shutdown(body: UpdateJobBody):
        installed_home()
        from desktop import shutdown
        try:
            # Synchronous gate write: no task admission can interleave.
            return shutdown.prepare(settings.data_dir, body.job)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/shutdown/release")
    async def release_shutdown(body: UpdateJobBody):
        installed_home()
        from desktop import shutdown
        shutdown.release(settings.data_dir, body.job)
        return {"released": True}

    @app.get("/api/desktop/update-notice")
    async def update_notice():
        # Same local Host/Origin boundary as Console, no privileged control data.
        return updates.notice(installed_home()) if install_home else {"supported": False}

    @router.get("/updates")
    async def update_status():
        return updates.status(installed_home()) if install_home else {"supported": False}

    @router.post("/updates/check")
    async def check_update():
        return await asyncio.to_thread(updates.check, installed_home())

    @router.post("/updates/preferences")
    async def update_preferences(body: UpdatePreferenceBody):
        return await asyncio.to_thread(updates.configure, installed_home(), body.auto_check)

    @router.post("/updates/install")
    async def update_install(body: UpdateBody):
        try:
            return await asyncio.to_thread(updates.request, installed_home(), body.version, confirmed=body.confirmed)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/updates/prepare")
    async def prepare_update(body: UpdateJobBody):
        try:
            return updates.prepare(installed_home(), body.job, app.state.db)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/updates/recover")
    async def recover_update(body: UpdateRecoveryBody):
        try:
            return await asyncio.to_thread(updates.recover, installed_home(), confirmed=body.confirmed)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, "恢复未完成：请确认更新器/安装器已退出，再使用恢复入口。") from exc

    if install_home:
        @app.on_event("startup")
        async def clear_abandoned_shutdown():
            from desktop import shutdown
            shutdown.recover(settings.data_dir)

        parent_lifespan = app.router.lifespan_context

        async def update_checks():
            import time
            while True:
                try:
                    from desktop.cleanup import retry
                    await asyncio.to_thread(retry, Path(install_home))
                    current = updates.status(Path(install_home))
                    if current["auto_check"] and time.time() - current["feed"].get("checked_at", 0) >= updates.CHECK_INTERVAL:
                        await asyncio.to_thread(updates.check, Path(install_home))
                except (OSError, ValueError, RuntimeError):
                    pass
                await asyncio.sleep(60)

        @asynccontextmanager
        async def lifespan(api):
            async with parent_lifespan(api) as state:
                checker = asyncio.create_task(update_checks(), name="desktop-update-checks")
                try:
                    yield state
                finally:
                    checker.cancel()
                    await asyncio.gather(checker, return_exceptions=True)
        app.router.lifespan_context = lifespan

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith("/api/setup"):
            # Default validation responses include raw input, possibly a key/code.
            return JSONResponse({"detail": "Invalid setup parameters", "errors": [
                {"location": list(error["loc"]), "type": error["type"]} for error in exc.errors()
            ]}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    def entry(client, transport):
        # Installed runtimes are launched through the stable home pointer.
        import os
        install_home = os.environ.get("CLICKCLICK_INSTALL_HOME")
        if install_home:
            from desktop.install import mcp_command
            command, args = mcp_command(Path(install_home))
        else:
            command, args = sys.executable, ["-m", "control_api.mcp", "--workspace", str(Path.cwd()),
                "--data-dir", str(settings.data_dir.resolve()), "--backend-url", base_url, "--no-start"]
        return server_entry(client=client, transport=transport,
                            base_url=http_base_url(settings) if transport == "http" else base_url,
                            token=local_token(settings), command=command, args=args)

    @router.get("/status")
    async def status():
        return {"api_model": model_summary(settings.data_dir), "guides": devices.GUIDES,
                "mcp_url": http_base_url(settings) + "/mcp/",
                "http_mcp_enabled": settings.mcp_http_enabled,
                "configuration_applies_after_restart": False,
                "guidance": "在设置页选择订阅登录或 API，新任务使用保存的模型和预算。"}

    @router.post("/model")
    async def model(body: ModelBody):
        try:
            return save_model(settings.data_dir, model=body.model, base_url=body.base_url,
                              api_key=body.api_key.get_secret_value())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/devices")
    async def device_inventory():
        try:
            return await devices.inventory()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(503, "无法读取 ADB 设备，请检查安装的设备工具。") from exc

    @router.post("/pair")
    async def pair(body: PairBody):
        try:
            return await devices.pair(body.endpoint, body.code.get_secret_value())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(503, "配对未完成，请检查同一 Wi-Fi、手机配对页面和当前配对码。") from exc

    @router.post("/connect")
    async def connect(body: ConnectBody):
        try:
            return await devices.connect(body.endpoint)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(503, "ADB 连接未完成，请检查实际连接端口和网络。") from exc

    @router.post("/mcp-connection")
    async def prepare_connection(body: ConnectionBody):
        try:
            return await asyncio.to_thread(connection_info, settings.data_dir, entry("generic", "stdio"), locale=body.locale)
        except (ValueError, OSError):
            raise HTTPException(503, "无法生成本机 MCP 接入信息，请检查 ClickClick 安装状态和数据目录权限。") from None

    app.include_router(router)
