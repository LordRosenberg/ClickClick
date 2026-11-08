# 设计决策与取舍

[首页](../README.zh-CN.md) · [English](design-decisions.md) · [架构](architecture.zh-CN.md) · [技术概览](reliability-design.zh-CN.md)

本页只索引方案理由，具体协议与配置由各专项详设维护；历史对照与未采纳方案单独保存。

## 角色策略

默认双角色推进、按需独立审核，减少固定审核开销；明确争议仍可升级。协议见[编排详设](task-orchestration.zh-CN.md)，历史匹配对照见[运行时研究](runtime-study.md)。

## 上下文预算

有界历史减小每轮输入，来源引用和回查避免以短摘要替代全部证据；更大窗口也可能增加累计输入。当前容量与配置见[记忆详设](memory-and-context.zh-CN.md)，历史预算对照见[研究记录](runtime-study.md)。

<a id="jev-摘要校验逐子句证据检查与一次修正隔离实验"></a>

## 可选 Jev 摘要校验

额外文字校验可拦截不忠实摘要，也增加延迟且覆盖有限。当前可选协议、降级与缓存见[记忆详设](memory-and-context.zh-CN.md#jev-文字忠实度校验隔离实验)；历史环境、校准及结果见[实验记录](memory-context-experiments.zh-CN.md)。

## Prompt cache 连续性

稳定提示词前缀与保留消息有利于复用缓存，频繁重写旧上下文可能以更短文本换来更多未缓存输入。需同时比较缓存、输出与额外调用；上下文布局见[记忆详设](memory-and-context.zh-CN.md)，计量见[可观测性](observability.zh-CN.md)。

## scrcpy、Collector 与 Python

像素与结构分工减少重复设备开销，有界窗口检查和重采兼顾一致性与动态页面可用性。具体采集方法见[一致性详设](observation-execution-consistency.zh-CN.md)，安装与诊断见[采集通路](scrcpy-observation.md)。

## 定向文本替换

把聚焦、替换与读回合为受支持字段的一次工具流程，减少模型往返；歧义或不支持字段不强行套用，读回也不证明保存。方法见[定向输入](observation-execution-consistency.zh-CN.md#定向文本替换与焦点一致性)。

## 原生节点绑定

保留节点并复核属性，缓解轻微布局偏移；不确定结果不自动回退旧坐标。支持范围与时序见[节点绑定](observation-execution-consistency.zh-CN.md#原生节点绑定与属性复核)，设备验证见[技术报告](native-node-click-20260920.md)。

## 按角色交付 skills

目录先行、阶段选择与角色投影减少无关正文，但仍需检查实际加载与动作采用；文件知识不等于执行权限。详见[交付设计](skill-delivery.zh-CN.md)与[自学习设计](skill-evolution.zh-CN.md)。

## 预算与指标

累积限额防止无界执行，明确增额保留失败历史；角色、请求、提交动作和物理子动作分别计量。限额见[编排](task-orchestration.zh-CN.md)，定义与评分见[评价体系](evaluation-design.zh-CN.md)。

## 文本树优化的验收边界

Reader 专用与其他未采纳表示属于历史实验，不作为当前能力或部署开关。原始结果与撤回依据保存在[记忆实验记录](memory-context-experiments.zh-CN.md#reader-pdf-observation-experiment)。

## HITL 提问与主动介入

模型保留意图判断，运行时处理等待和并发资格；已执行效果不随变更自动回滚。回复入口、迟到响应和在线重规划见[HITL 详设](hitl-design.zh-CN.md)。

## 通用助手投屏预览

浏览器展示与任务执行解耦，复用已有帧而不建立第二条采集／输入通路。宿主能力影响展示，不影响后台任务；见[可观测性](observability.zh-CN.md)。

## 异常恢复与执行幂等

事务保存意图、回执和完整状态，Driver 按 ID 去重；不把 SQLite 提交当成手机效果的原子提交。unknown 交给原角色结合新观测决策，避免猜测后重放；详见[恢复设计](crash-recovery.zh-CN.md)。
