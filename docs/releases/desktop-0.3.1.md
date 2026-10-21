# ClickClick 0.3.1

Change Android task requirements while they run, and watch progress alongside your assistant.

## Revise a running task through your assistant

- With HITL enabled, tell your assistant what changed. ClickClick acknowledges the update, then keeps or revises the plan using the latest requirements and actual progress.
- Updates can interrupt model waiting. Undispatched obsolete actions stop; already dispatched actions may complete and existing external effects are not automatically reversed.
- The original request, changes and execution records remain inspectable. Paused tasks stay paused, and changes after completion create linked follow-up tasks.
- Assistant-guided first installations default HITL on; other installations default it off. Existing saved settings take precedence. MCP connection alone does not change the switch, and each task retains its creation-time setting.

## Screen preview alongside your conversation

- Assistants with an in-app browser can automatically open a local Android task's preview after delegation, with recent phone pixels and model output side by side.
- Select **Screen preview** in task details, or **Task details** in the preview, to navigate in either direction. English and Chinese are available.
- Preview is read-only and labels retained images after pause or completion. Unsupported previews fall back to task details; a missing browser does not stop phone execution.

## Continue a budget-exhausted task

- Ordinary tasks default to 200 Executor decisions and 1000 model requests, configurable in Console.
- Budget exhaustion remains failed. A safe budget checkpoint can continue after an explicit extension, keeping the same task, earlier attempts, execution history and cumulative consumption.
- Requirement updates do not reset counters or extend deadlines. Ordinary failures are not resumable through this feature, and evaluation results are not overwritten by continuation.

Choose the Windows x86_64, macOS Apple Silicon or macOS Intel installer below. Upgrading retains model configuration, task records and saved Skills. See [installation](https://github.com/LordRosenberg/ClickClick/blob/main/docs/desktop-setup.zh-CN.md) and [HITL usage](https://github.com/LordRosenberg/ClickClick/blob/main/docs/hitl.md).

**macOS:** Packages are not Developer ID signed or Apple notarized. Gatekeeper may prevent opening them; source deployment remains available. This release does not claim signing or notarization verification.

---

通过助手调整正在执行的手机任务，并在对话旁查看手机画面。

## 执行中主动调整任务

- 开启 HITL 后，直接告诉助手新的要求。ClickClick 及时确认收到，再根据最新要求与实际进展保留或修订计划。
- 模型等待期间也能处理修改。尚未派发的旧动作会停止；已派发操作可能完成，已产生的外部结果不会自动撤销。
- 原始要求、历次修改与执行记录持续保留。暂停任务仍保持暂停，完成后的修改创建关联后续任务。
- 助手引导首次安装默认开启 HITL，其他安装默认关闭；已有保存的选择优先。连接 MCP 不改变开关，既有任务保持创建时的设置。

## 助手侧投屏预览

- 支持内置浏览器的助手派发本机 Android 任务后，可自动打开预览页，并排展示最近手机画面与模型输出。
- 任务详情的「投屏预览」与预览页的「任务详情」支持双向跳转，提供中英文界面。
- 预览只读，暂停或结束后明确标记最后画面。不支持投屏时回退详情，浏览器不可用不影响手机执行。

## 预算耗尽后继续

- 普通任务默认 200 次 Executor 决策、1000 次模型请求，可在 Console 配置。
- 预算耗尽仍记为失败；安全检查点允许明确追加预算后继续，保留同一任务、原执行尝试、操作历史和累计消耗。
- 修改要求不会重置计数或自动延长截止时间。普通失败不开放原地续跑，评测成绩不因续跑改写。

请选择 Windows x86_64、macOS Apple Silicon 或 macOS Intel 安装器。升级保留模型配置、任务记录和 Skills。参见[安装指南](https://github.com/LordRosenberg/ClickClick/blob/main/docs/desktop-setup.zh-CN.md)和 [HITL 使用指南](https://github.com/LordRosenberg/ClickClick/blob/main/docs/hitl.zh-CN.md)。

**macOS：安装包未经过 Developer ID 签名和 Apple 公证，可能被 Gatekeeper 阻止打开。** 可选择源码部署；本版不宣称完成签名或公证验收。
