# ClickClick 0.3.2

Continue eligible phone tasks after an unexpected backend interruption, with durable execution records and restored agent context.

## Interruption recovery

- New tasks default to bounded automatic recovery. Manual and disabled policies are available; the policy is frozen at task creation. Existing tasks without recovery metadata are not automatically made resumable.
- Recovery retains the task ID, plan, notes, dialogue, loaded Skills and cumulative budget. It isolates the old input chain before continuing on the original device.
- A dispatched action without a reliable receipt is recorded as **unknown**, never automatically replayed. The original role receives a fresh observation and decides the next action; recovery does not claim exactly-once business effects or roll back phone operations.
- Console shows recovery phases, device waiting, blocking reasons and unknown operations. Assistants reuse `resume_task`; acceptance is followed by task-status checks.
- Pause, cancellation, HITL settings, deadlines and budget limits remain in force. Terminal tasks stay terminal.

Updated bilingual [architecture](https://github.com/LordRosenberg/ClickClick/blob/main/docs/architecture.md), technical overview and design decisions explain the persistence sequence, state machine, configuration and limitations. Validation includes automated regression tests and MobileWorld interruption/boundary checks; physical-phone disconnect/reboot and macOS input-process recovery were not separately accepted.

Choose the Windows x86_64, macOS Apple Silicon or macOS Intel installer below and verify its adjacent SHA-256. Upgrading retains saved configuration and task records. Database migrations create a consistency backup when upgrading existing task data; keep the new database and journals if troubleshooting a rollback instead of restoring old state over external effects.

**macOS:** Packages are not Developer ID signed or Apple notarized. Gatekeeper may prevent opening them; source deployment remains available.

---

## 异常中断后继续任务

- 新任务默认启用有界自动恢复，也可选择手动或关闭，策略在创建时固定。缺少恢复元数据的旧任务不会强行续跑。
- 保留原任务 ID、计划、笔记、对话、已加载 Skills 和累计预算，隔离旧输入链后在原设备继续。
- 已派发动作缺少可靠回执时记为 **unknown**，不会自动重放；取得最新观测后由原角色模型决定下一步。不承诺业务效果恰好一次，不自动回滚手机操作。
- Console 展示恢复阶段、设备等待、阻塞原因和未知操作。助手沿用 `resume_task`，受理后需继续查询任务确认进展。
- 暂停、取消、HITL 设置、截止时间和预算限制继续有效，已结束任务不会被恢复复活。

中英文[架构详解](https://github.com/LordRosenberg/ClickClick/blob/main/docs/architecture.zh-CN.md)、技术概览与设计取舍已补充持久化时序、状态机、配置和能力边界。已进行自动回归及 MobileWorld 中断与边界验证；物理手机断连/重启和 macOS 输入进程恢复尚未单独验收。

提供 Windows x86_64、macOS Apple Silicon 和 Intel 安装器及 SHA-256。升级保留配置和任务记录，旧任务库迁移前生成一致性备份；回滚排障请保留新库和 journal，不用旧状态覆盖已经发生的外部效果。

**macOS：安装包未经过 Developer ID 签名和 Apple 公证，可能被 Gatekeeper 阻止打开；可选择源码部署。**
