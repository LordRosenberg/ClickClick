# ClickClick 0.3.3

Recover interrupted GUI script steps and verify ambiguous submissions before continuing.

## GUI scripts and task inspection

- The Console Workbench brings the device preview, current task and event feed together. Enable script recording for an individual task when you want to reuse its GUI workflow.
- Replay can resolve a local blocker or hand the current step to Executor. If an App update invalidates the old route, Executor can ask Planner to revise it. The runner verifies the current step before advancing to the next one.
- When a submission may already have reached the App, replay pauses further dispatch while it checks the result. Where the script declares a matching business record, a new receipt ID can confirm the step even if the confirmation page is missing. Resume continues verification on the same attempt.

Validation included automated replay and recovery tests and Android emulator runs for route changes, step handoff, and interruptions around dispatch. Some native dialogs still require manual handling.

Downloads are available for Windows x86_64, macOS Apple Silicon and macOS Intel. Verify the installer against its accompanying SHA-256 file. The macOS packages are unsigned and unnotarized. Gatekeeper may prevent opening them. [Getting started](https://github.com/LordRosenberg/ClickClick/blob/desktop-v0.3.3/README.md)

---

## GUI 脚本与任务工作台

- Console 工作台把设备预览、当前任务和事件放在同一页面。需要复用 GUI 流程时，可为单个任务开启脚本录制。
- 回放遇到局部阻塞时，可先有限接管。App 改版导致原路径失效时，Executor 可请求 Planner 调整路径。运行器核实当前步骤的结果后，才进入下一步。
- 提交可能已到达 App 时，回放暂停派发并核对结果。脚本配置了可对应本次提交的记录后，即使缺少确认页，也能用新编号核验。点击「恢复」会继续核对同一次尝试。

本版通过了自动回放与恢复回归，并在 Android 模拟器验证了改版重规划、逐步交还和派发前后中断。部分原生弹窗仍需要人工处理。

提供 Windows x86_64、macOS Apple Silicon 和 macOS Intel 安装包及 SHA-256。macOS 安装包未签名、未公证，可能被 Gatekeeper 阻止打开。[桌面安装指南](https://github.com/LordRosenberg/ClickClick/blob/desktop-v0.3.3/docs/desktop-setup.zh-CN.md)
