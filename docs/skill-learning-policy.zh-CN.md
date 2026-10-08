# 学习成本与贡献边界

[Skills 自进化设计](skill-evolution.zh-CN.md) · [系统架构](architecture.zh-CN.md)

普通任务只负责完成用户目标。必要的纠错/换路径仍属于正常执行；结束后的额外重试和对比探索属于研究，不会自动启动。`skill_learn=true`、`CLICKCLICK_SKILL_LEARNING_MODE` 和旧 `force` 参数都不能触发任务后模型调用。终态机械记录一条幂等本地 `learning_clue`，不读写设备、不调用模型、不写 pending。成功、失败、取消均可记录；记录失败不改变任务结果。

## 本地设置与贡献

`GET/PUT /api/learning/preferences` 保存独立的 `local_recording`（默认 true）和 `contribution_enabled`（默认 false）。关闭本地记录影响后续收集，不删除已有运行历史。关闭贡献立即阻止已有记录的后续导出。

`GET /api/tasks/{task_id}/learning-contribution` 只有当前贡献同意开启时可用。报告通过白名单重建，只有随机报告 ID、实际 App 包名、终态、枚举机械线索、动作类型计数和运行计数。没有指令、失败原文、笔记、树、截图、输入文字/坐标、任务 ID、设备序列号或原始内容 hash；未知计数保留 null。报告仍包含使用哪些 App 和结果/计数，页面先预览，再由用户下载。

这是本地导出协议，当前没有自动网络上传、积分结算或官方云接收地址。贡献同意不授权个人探索，也不自动调用模型。任务列表不再提供结束后自动学习选项。

## 个人定向优化

任务详情单独提供“优化我的工作流”。先确认模型请求、动作和总时限，以及两项同意：使用当前配置模型通道的开销、允许本次探索实际操作设备。`POST /api/tasks/{task_id}/learn` 的请求体：

```json
{
  "accept_model_cost": true,
  "allow_device_operations": true,
  "max_calls": 24,
  "max_actions": 30,
  "max_seconds": 900
}
```

请求数范围 12–64、动作 1–100、总时限 30–1800 秒，默认 24/30/900。不同模型和输出长度使 token/金额无法从请求上限精确预测；这里接受的是请求/动作/时间上限，不是固定费用报价。缺任何同意均在解析设备或建立后台前拒绝。`billing_scope` 不接受调用方指定为官方。总时限包含该个人研究 job；provider 重试、阶段转换不重置额度。

个人研究始终使用 task-conditioned Learner 和独立 Reviewer，不能转入旧文本总结绕过六项要求。保留设备互斥、取消、真实环境变化日志和 cleanup 约束；用户可停止本次优化。结果只进入本地 pending。按实际声明走局部证据准入或测量效用验证；相应证据、六项审查或版本绑定不足时保持 blocked。局部准入不证明整体收益，用户点击同意不能代替所需证据；正式库不会热写。两种准入与发布边界见[独立审查设计](skill-evolution.zh-CN.md#独立审查局部事实与整任务收益分开)。
