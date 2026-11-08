# 记忆与上下文历史实验

[当前详设](memory-and-context.zh-CN.md) · [English](memory-context-experiments.md)

本页将历史实验与当前记忆协议分开。完整开发实验日记、原始轨迹、本机命令与内部证据保留在开发仓库。

## Jev 文字忠实度实验

历史研究使用绑定来源的摘要检查与受控错误回放，分析无依据断言、覆盖缺口、修正成本和降级行为。人工注入的错误与自然错误分开记录；小规模回放不证明生产召回率，也不能直接归因为基准成功率提升。

当前 Jev 可选模式、阈值、修正次数、来源解析和失败策略见[记忆详设](memory-and-context.zh-CN.md#可选-jev-文字忠实度校验)，不从旧实验设置推断当前默认值。Jev 校验文字是否忠实于来源，不读取图片像素，也不证明业务结果正确。

## Reader 与文本树实验

<a id="reader-pdf-observation-experiment"></a>
<a id="reader-and-text-tree-experiments"></a>

曾探索 Reader 专用处理、页码标注和文本树表示，相关方案已经退役，不作为当前能力或推荐配置。当前 snapshot、sequence 与原生 detail 的行为统一见[观测与执行详设](observation-execution-consistency.zh-CN.md)。

## 角色与上下文预算对照

已公开的[运行时研究](runtime-study.md)保留角色策略和 16k/24k 上下文预算的配对比较，以及样本量、消耗、未评分尝试和限制。这些历史对照不单独证明后来每项记忆机制的收益。

基准设置和汇总成绩见[评测指南](evaluation.zh-CN.md)。历史实验配置不覆盖当前默认值，也不替换已有评分结果。
