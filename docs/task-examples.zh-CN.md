# 任务实例

[首页](../README.zh-CN.md) · [English](task-examples.md) · [演示视频](demos.md) · [部署](deployment.zh-CN.md)

用自己的话描述想完成的事，ClickClick 会规划步骤，并根据实际页面执行。下面的例子展示可完成的任务，不是必须遵循的提示词模板；你可以自由替换措辞、应用和数据。

## 跨应用转录

例如，你可以直接说：

> 把 Markor 当前笔记里的食谱添加到 Broccoli。

有特别要求时，可以补充“保留原文”或“不要修改已有食谱”等约束。Agent 负责读取来源、切换应用和填写表单。[观看三份食谱的完整执行](assets/demos/notes-to-recipes.mp4)。

## 从图片提取结构化数据

> 在 Simple Gallery Pro 中读取 expenses.jpg，将每条费用的名称、金额、类别和备注录入 Pro Expense。

Agent 读取来源图片，将内容带入目标应用，再检查创建的记录。[观看执行视频](assets/demos/image-to-expenses.mp4)。

## 配置重复日历事件

例如：“在日历里添加明天上午 9 点的晨会，持续 30 分钟，每个工作日重复。”Agent 将这些要求转换为页面上的日期、时间和重复设置。[观看完整配置过程](assets/demos/recurring-calendar-event.mp4)。

## 核验任务结果

新增任务检查记录数量、各个必填字段及原记录是否保留；删除任务同时检查目标移除和剩余记录。Console 提供操作历史、截图和输入回执，最终保存情况以应用状态为准。

<a id="metric-definitions"></a>
### 记录级指标

所需记录全部指定字段正确，或完成指定删除，计为真阳性 TP；额外或错误结果计为 FP；缺失结果计为 FN。精确率为 TP/(TP+FP)，召回率为 TP/(TP+FN)。非目标记录的意外变化单独统计。[受控任务实验](runtime-study.md)给出字段与保留性审计实例。

<a id="try-a-multi-record-workflow"></a>
## 可选：用固定数据检查多条记录录入

以下示例提供两份固定食谱，方便运行后逐字段核对，也便于比较不同配置。字段列表只是本例的输入数据，不是 ClickClick 要求的提问格式。中英文指南使用相同英文数据。

1. 完成 [README 设备初始化](../README.zh-CN.md#部署)。通过 Broccoli [官方项目](https://github.com/flauschtrud/broccoli)链接的分发渠道安装应用，手动打开并完成引导。跨应用模式还需安装 [Markor](https://github.com/gsantner/markor)、完成引导并授权所需文件访问。
2. 使用测试集合，确认下方两个标题不存在；再次运行时为两者加上相同新后缀并始终保持一致。提交前记录原食谱数量。
3. 直接录入时将以下指令粘贴到 Console，选择一个设备和两个角色模型，点击 **submit**。

```text
In Broccoli, create exactly these two recipes. Preserve all other recipes.
Keep the supplied text unchanged. Save each recipe and check its saved fields.

Recipe 1
Title: ClickClick Demo Oats
Description: A small breakfast test recipe.
Servings: 1
Preparation time: 10 minutes
Source: https://example.com/clickclick-demo
Ingredients:
40 g oats
200 ml water
Directions:
Bring the water to a boil. Add the oats and simmer for 5 minutes.
Favorite: no

Recipe 2
Title: ClickClick Demo Salad
Description: A small lunch test recipe.
Servings: 2
Preparation time: 15 minutes
Source: https://example.com/clickclick-demo
Ingredients:
1 cucumber
2 tomatoes
Directions:
Wash and chop the vegetables. Mix them in a bowl.
Favorite: yes
```

4. 跨应用模式先将两份食谱保存为 Markor 的 `clickclick-demo.md`，保持笔记打开。提交：**Read the two recipes in the currently open Markor note clickclick-demo.md, then create exactly those two recipes in Broccoli. Preserve their text and all existing recipes; save and check the new records.** 这会测试源信息采集及应用切换，而不是直接在指令中提供字段值。
5. 在 Console 检查阶段转换、active skills 和实际模型输入，确认切换应用后源信息仍可用，并查看可用的输入回执。在 Broccoli 确认数量恰好增加二、八项字段均匹配、原记录未变。记录任务耗时和模型用量，缺失用量保持未知。

比较配置时，恢复相同集合和源笔记，固定应用版本与任务指令，并保留每次尝试。基准环境配置另见[评测指南](evaluation.zh-CN.md)。
