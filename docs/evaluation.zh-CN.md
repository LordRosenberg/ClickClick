# 评测与复现

[首页](../README.zh-CN.md) · [English](evaluation.md) · [115/116 评测报告](androidworld-results-20260921.md) · [AndroidWorld 适配器](androidworld-benchmark.md)

可以从两条路径开始：通过 Console 验证自己的任务，或将 ClickClick 接入 AndroidWorld，使用应用状态评分。公布的 115/116 使用单独冻结的全量协议，具体条件见评测报告。

## 验证自己的任务

1. 完成[部署](deployment.zh-CN.md)，用自然语言描述要完成的任务；[任务实例](task-examples.zh-CN.md)可供参考。
2. 准备已知来源数据，记录目标应用的初始状态。
3. 提交任务并记录模型、技能与任务限制；做对照评测时保持这些条件一致。
4. 检查应用保存结果及 Console 轨迹，核对字段、额外结果和非目标记录。
5. 比较不同配置时恢复相同初始状态，并保留每次尝试。

任务结束、设备派发成功和应用结果正确是不同指标。一个字段保存正确与整个集合处理完整，需要的检查也可能不同。

## 接入 AndroidWorld

按照 AndroidWorld [官方仓库](https://github.com/google-research/android_world)准备 Android API 33 模拟器与应用环境。在同一 Python 环境中安装可导入的 ClickClick 和 AndroidWorld，配置模型，并让两者连接同一模拟器。

按照[适配器指南](androidworld-benchmark.md)，将 `agent.integrations.android_world.ClickClickAgent` 注册到 AndroidWorld 的 Agent 工厂。首次可执行：

```bash
python run.py \
  --suite_family=android_world \
  --agent_name=clickclick \
  --tasks=ContactsAddContact \
  --perform_emulator_setup
```

完成注册后在 AndroidWorld 仓库中运行。`--perform_emulator_setup` 用于首次准备，后续运行使用已初始化模拟器。适配器调用 ClickClick 运行时，AndroidWorld 管理初始化、外层步数、成功判定和清理。

## 复现报告中的实验

[9 月 21 日评测报告](androidworld-results-20260921.md)列出冻结运行时、模型、Collector 摘要、技能、种子、预算、动作空间和结果选择。精确复现需要对应冻结 fixture、技能 overlay 与全量 runner；这些材料目前未随 public 源码镜像提供。公开的逐步适配器可用于开展新评测，但与该次实验的 runner 不完全相同。

结果应附带应用和 Android 版本、模型设置、运行时与技能版本、任务参数、重试/选择规则，以及初始化与评分实现。复合动作同时保留提交动作和物理子动作计数。

## 指标

| 维度 | 记录内容 |
| --- | --- |
| 正确性 | 外部成功分数、要求字段、额外结果与非目标记录保留。 |
| 执行 | 提交动作、物理子动作、模型调用和预算内完成情况。 |
| 时间 | 任务/worker 耗时、模型延迟、采集与压缩时间。 |
| 用量 | 提供方返回的输入、缓存输入和输出，估算值单独标识。 |
| 可靠性 | 初始化、模型传输、采集和清理错误。 |

提供方缺失用量保持未知，基础设施中断与任务失败分别记录。原始结果及后续选择应能够区分。

## 检查证据

Console 关联展示调用、观测和工具回执。每个计分任务保留目标、配置、初始/最终状态、结果及执行轨迹。[可观测性指南](observability.zh-CN.md)说明检查方法，[演示来源](demos.md#recording-provenance)展示媒体与成功任务的关联。
