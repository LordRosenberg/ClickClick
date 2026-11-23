# 学习成本与贡献边界

[Skills 自进化设计](skill-evolution.zh-CN.md) · [English](skill-learning-policy.md) · [系统架构](architecture.zh-CN.md)

普通任务沿用已有 Planner 和 Executor 完成用户目标。默认的终态收集仅生成幂等的本地 `learning_clue`，无需操作设备、调用模型或写入候选。成功、失败、取消均可记录，任务结果独立于记录是否写入。旧版 `skill_learn=true`、`CLICKCLICK_SKILL_LEARNING_MODE` 和 `force` 参数已失效。

## 本地设置与贡献

`GET/PUT /api/learning/preferences` 保存独立的 `local_recording`（默认 true）和 `contribution_enabled`（默认 false）。关闭本地记录影响后续收集，不删除已有运行历史。关闭贡献立即阻止已有记录的后续导出。

`GET /api/tasks/{task_id}/learning-contribution` 只有当前贡献同意开启时可用。报告通过白名单重建，只有随机报告 ID、实际 App 包名、终态、枚举机械线索、动作类型计数和运行计数。没有指令、失败原文、笔记、树、截图、输入文字/坐标、任务 ID、设备序列号或原始内容 hash；未知计数保留 null。报告仍包含使用哪些 App 和结果/计数，页面先预览，再由用户下载。

这是本地导出协议，当前没有自动网络上传、积分结算或官方云接收地址。贡献同意不授权个人探索，也不自动调用模型。任务列表不再提供结束后自动学习选项。

## Android 工作流版本审查

Android 版本审查需主动开启。设置 `CLICKCLICK_SKILL_VERSION_AUDIT_ENABLED=true` 并开启本地记录后，运行时会在 App 位于前台时，为实际交付给 Executor 的工作流 Skill 版本记录 Android 构建号。首次观测建立基线。后续观测到构建变化且有配对动作证据时，任务结束后可启动同一个 Learner 的只读流程。

后台学习使用已配置的学习请求和时间上限，设备动作额度为零。Learner 先看简明时间线，再按需多轮读取原始动作、回执、观测和已有的历史截图。候选生成和独立评审共用这份预算。结论限于轨迹证据覆盖的 App 和路径。需要设备探索或新执行复验才能解决的缺口，留待显式授权的研究处理。构建变化和旁路成功都可以作为诊断线索。

任务的 `GET /api/tasks/{task_id}/skill-links` 会返回版本记录 `version_audits`、后台运行记录 `version_audit_run` 以及该任务的待审候选。后台版本审查保留人工审批。整条 Skill 的删除需要独立证据覆盖其声明范围，并在批准时重查受审的精确版本。批准后原文件移出正式库并归档。

## 显式启动 Skills 自学习

在已结束任务的详情中打开「Skills 自学习」，确认模型开销上限和测试设备操作授权。Console 向 `POST /api/tasks/{task_id}/learn` 发送以下请求。

```json
{
  "accept_model_cost": true,
  "allow_device_operations": true,
  "run_regression": true,
  "max_calls": 24,
  "max_actions": 30,
  "max_seconds": 900
}
```

请求数范围 12–64、动作 1–100、总时限 30–1800 秒，默认 24/30/900。不同模型和输出长度使 token/金额无法从请求上限精确预测；这里接受的是请求/动作/时间上限，不是固定费用报价。缺任何同意均在解析设备或建立后台前拒绝。`billing_scope` 不接受调用方指定为官方。总时限包含该个人研究 job；provider 重试、阶段转换不重置额度。

配对回归通过 `CLICKCLICK_SKILL_LOOP_FIXTURE_JSON` 配置专用模拟器和独立的 ADB 服务，也可读取与原录制任务绑定且可核验的脚本接管线索。后台选择一项经过独立审查的 App 技能新增或修订，优先复用已验证用例，缺少用例时补录并验证初态准备脚本。配对试验使用同一 App 构建、复原后的初态和任务条件。报告绑定候选正文、基线库与实际加载的 Skill 指南。通过发布校验后，符合条件的单 App 工作流低影响改动可自动发布，已验证的高影响改动进入待审批。证据不足或验证失败时保留生效版本。 [Skills 自学习详设](skill-evolution.zh-CN.md) · [GUI 脚本详设](gui-script-design.zh-CN.md)

API 调用方省略 `run_regression` 或传 `false` 时，走原有的手动研究入口。该入口仍使用任务条件化 Learner 和独立 Reviewer，保留设备互斥、取消、环境变化记录与清理约束。符合条件的结果进入本地待审批队列。两个入口均核对声明所需的证据与版本绑定。

## 官方后台闭环

运维在自己的服务/模型配置下接收用户主动提供的报告，先校验、去重、聚合：

```powershell
python -m evaluation.learning_inbox report1.json report2.json --output issues.json
```

同报告 ID 的重复不增加次数；内容冲突或额外原始字段被拒绝。聚合按曾出现的 App/机械信号排序；关联不证明该 App 是失败根因，状态固定为 `needs_operator_reproduction`，不调用模型、设备或发布。统计无法说明语义根因，也不能还原用户数据；因此运维先在自有测试环境建立可复现的问题，再使用现有 AndroidWorld/evaluation 学习入口探索和验收。没有复现证据的报告保留为线索。运维实验使用运维自己的模型通道，用户端不会自动调度这些工作。

官方公共技能仍须经过根因、通用性、冲突、退化风险、简洁性、边际价值六项评审，并按声明范围核对相应准入证据。局部知识可按来源证据审查；测量整体收益需要冻结完整库/单候选增量及相应同族变体、near-miss、相关正常任务覆盖，不能靠局部准入替代。当前继续人工发布，批准前重查正文、库、运行合同和环境绑定，长期自动接纳不会降低所需标准。公网传输、认证、发布推送是后续官方服务集成；本页的贡献功能仅实现 local export + operator inbox，不宣称部署了云后台。学习器的独立实验结果与限制见[Skills 自进化](skill-evolution.zh-CN.md)。
