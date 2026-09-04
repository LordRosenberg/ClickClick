# Process Supervision (Web / Agent / Driver)

ClickClick MVP runs as three local processes (plus optional scrcpy).

```
┌─────────────┐   HTTP    ┌──────────────────┐  HTTP RPC  ┌──────────────┐
│ Console Web │──────────▶│ Control API      │───────────▶│ Driver       │
│ (static)    │           │ (+ Agent orch.)  │            │ (ADB/Portal) │
└─────────────┘           └──────────────────┘            └──────┬───────┘
                                                                  │
                                                                  ▼
                                                               Phone
```

In this MVP the **Agent orchestrator is embedded in the Control API process** for simpler local ops,
while the **Driver remains a separate process**. A standalone `clickclick-agent` CLI remains available
for one-shot runs.

> 编排模型为 **Reviewer → Planner → Executor 三角色闭环**：任务启动时 Reviewer 先定义不可变的
> 任务合同（`must_happen` / `final_ui_state` / `answer`）；随后每个外层步骤由 Planner 滚动生成
> 一个语义子目标，Executor 每步只执行一个 typed action 或报告认知边界
> （`request_review` / `request_replan`）；到达边界时 Reviewer 基于精确证据句柄裁决
> `accept / retry / replan / done / blocked`，并唯一写入语义进展、事实与答案。
> 显式 `AgentState`（contract + task_memory + step_number）是真相，崩溃可从 DB 重建续跑。
> 硬护栏：`max_steps`（默认 50）或角色调用上限（默认 200）耗尽 → 显式失败；
> Harness 不按文案或计数判断语义重复，恢复方向由模型角色决定。详见根 README「Agent 编排层」。

## Start order

```bash
# 1) Driver (fixture for offline demo)
export CLICKCLICK_USE_FIXTURE_DRIVER=1
clickclick-driver --port 8765

# 2) Control API + Console
export CLICKCLICK_DRIVER_URL=http://127.0.0.1:8765
clickclick-api
# open http://127.0.0.1:8080
```

> Console Live 镜像无需手动起 scrcpy：Control API / Driver 自动推送仓库内 vendored
> `scrcpy-server` 并按需建立设备级共享会话（详见 `docs/accessibility-collector-setup.md`「Live mirror」）。

## Real device

See `docs/accessibility-collector-setup.md`. Unset `CLICKCLICK_USE_FIXTURE_DRIVER` and ensure the
Accessibility Collector + ADB are ready.

## Agent read tools and tracing

Reviewer / Planner / Executor each use a fixed, role-scoped tool catalog: `observe_screen(mode=current)` captures one globally aligned frame/tree; `mode=temporal` captures two or three ordered full-screen frames and marks only the ending frame/tree as the action coordinate reference. Planner/Executor additionally have `load_skill` / `search_skills`; Executor also has bounded `search_installed_apps(query)` discovery. Device-mutating primitives remain outside the registry and are dispatched once by the Harness action transaction after terminal submit.

Each role invocation persists ordered `llm_rounds[]` and `tool_calls[]`. Console’s **Agent Calls** waterfall shows live start/finish/failure state, redacted arguments/results, temporal artifacts, model/tool/overhead timing, input/cache-read/cache-write/output tokens, and prefix/catalog hashes. A missing cache metric renders as unavailable; explicit zero is a measured miss.

读工具、时序观测和多模态附件属于固定运行时能力，不通过环境变量开关。

The bounded screencap sampler is the fallback when a continuous temporal provider is absent. If a provider exposes `observe_temporal(request)`, the same agent contract uses it and forwards the exact monotonic last-action anchor.

Synthetic measurement coverage compares the ordinary one-submit path with an observation invocation: one versus two LLM rounds, zero versus one non-terminal read call, per-round cache reads, image count, tool time, and aggregate model latency. Real-device/provider latency should be evaluated from the same Console fields because screencap and ring-buffer costs are hardware-dependent.
