# MobileWorld GUI-only: 106/117 · 澄清对照 112/117

本次提交包含 117 道 GUI-only 原题成绩，以及针对原题存在疑问的 7 道澄清题补充结果。

- 原题成绩：**106/117，90.60%**。
- 澄清对照成绩：**112/117，95.73%**。

澄清对照将这 7 道题的补充结果放入对应原题位置，其余任务采用原题结果，分母保持 117。两组分别提供逐题轨迹、截图视频及榜单条目。

ClickClick 是一个开源 Android Agent 平台，本次评测使用 GPT-5.6-Sol。其核心是将可修订规划、持久记忆、可复用技能和设备反馈组织成完整执行流程：结合截图与 UI 树理解页面，根据实际操作结果调整计划，通过带来源引用的记忆和原始记录回读保留跨应用任务中的关键细节，并按应用、设备和通用工作流加载适用技能，减少重复探索。Web Console 可查看实际模型输入、决策、工具调用和关联截图，使长任务的执行过程可追踪、可检查。相关测试成绩：Androidworld **115/116，成功率 99.14%**。

每题预算为 50 个 MobileWorld 回合、2400 秒。用户交互和 MCP 任务未评测。视频由官方打包脚本将逐步截图合成。

## 澄清内容

| 任务 | 原题疑问 | 澄清内容 |
| --- | --- | --- |
| CheckDeduplicatedEventsTask | 日历事件去重口径不明确。 | 同一周期事件在范围内只计一次；跨日事件也只计一次。 |
| CartManagementTask | “短袖T恤衬衫”的删除范围存在歧义。 | 短袖、T恤、衬衫满足任一条件均删除。 |
| DownloadSendReceiptTask | 附件名与实际文件名不一致。 | 将 receipts.jpg 修正为 receipt.jpg。 |
| SendFormsTask | October 3rd onward 的截止日期不明确。 | 包含 10 月 3 日及之后全部相关邮件，不以当天为上界。 |
| InvoiceReceiptCopyAskUserTask | 目标文件夹路径未明确。 | 明确 Documents/expense/invoice；不存在时先创建。 |
| MastodonServerInfoReportTask | 题干未提供 owner 登录信息。 | 补充官方公开测试账号信息，通过界面登录。 |
| ReviewPaperEmailTask | 题干目录名与实际目录名不一致。 | 统一为 review_v2，保持其他要求不变。 |

项目：https://github.com/LordRosenberg/ClickClick

## 评测配置与逐题证据

评测日期：2026-10-07。上游任务集：`e41d1478e252325c513003d3d191b4c164b4af2c`；Android API 34，使用官方应用与后端服务。117 道原题均保留数值评分，未通过的原题仍计入分母。原题与澄清对照分别为 106 通过、11 失败和 112 通过、5 失败。澄清对照是补充结果与原题结果组成的对照，不是另一轮独立的 117 题全量测试。

初始化核对应用就绪、时钟、适用任务的照片选择器资源及官方公开测试账号信息的模型交付；登录和账号切换仍由 Agent 操作界面完成。Jev 使用 enforce 模式，服务故障按 bypass 降级，并保留内部用量审计。

[逐题评分与完整公开数据](mobileworld-results-20261007.json) · [截图与角色决策网站](https://lordrosenberg.github.io/ClickClick/mobileworld/)

网站的每个任务展示本次实际执行的 Planner、Executor、Reviewer 决策及设备截图；未增加虚构的角色事件或模型调用。所有 117 道原题均可查看，澄清补充共 7 道。公开资料不包含原始数据库、供应商请求、私有凭据或内部审计。

榜单对比采用 2026-10-07 的官方公开 50 回合 GUI-only 项目；最高公开项目 Qwen-UI-Agent 为 82.1%。这是成绩对比，未宣称已被官方榜单接受。[官方榜单数据](https://github.com/Tongyi-MAI/MobileWorld/blob/main/site/leaderboard.json)。
