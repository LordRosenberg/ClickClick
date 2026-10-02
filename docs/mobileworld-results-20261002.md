# MobileWorld GUI-only: 95.73%

[Results, leaderboard and six demos](https://lordrosenberg.github.io/ClickClick/mobileworld/) · [Public score data](mobileworld-results-20261002.json) · [AndroidWorld](androidworld-results-20260921.md)

ClickClick passes **112/117 tasks (95.73%) with locally clarified instructions**. The **original-wording result is 106/117 (90.60%)**. Both refer to the same 117 GUI-only tasks in `full-gui-paired-20261001-v2`, completed October 2, 2026. No task is removed from the denominator. The difference is **six passes, or 5.13 percentage points**.

ClickClick 在本地澄清指令后通过 **112/117（95.73%）**；**原题措辞未修改版也达到 106/117（90.60%）**。分母均为同一组 117 道纯 GUI 任务，没有剔除失败项。两个口径均披露两道 Mastodon 任务补充官方测试账号环境信息；“原题未修改”指题目措辞，不意味着整个执行环境没有适配。

## Configuration and score construction

| Item | Configuration |
| --- | --- |
| Task set | MobileWorld's 117 GUI-only tasks; excludes 44 user-interaction and 40 MCP tasks |
| Upstream source | [`e41d1478e252325c513003d3d191b4c164b4af2c`](https://github.com/Tongyi-MAI/MobileWorld/tree/e41d1478e252325c513003d3d191b4c164b4af2c) |
| Model | `chatgpt/gpt-5.6-sol`, high reasoning |
| Android | API 34 / Android 14, official app fixtures and backend services |
| Limits | At most 50 MobileWorld rounds per task, with a 2,400-second task limit; device actions and rounds are separate counts |
| Agent inputs | Skills and prompts frozen before the run; application state inspected and operated through GUI |
| Original-wording score | 106 passed, 11 failed, 117 total |
| Clarified comparison | 112 passed, 5 failed, 117 total |
| Changed cases | Eight wording clarifications and one additional official-account context variant; eight of these nine executions pass |

The original and changed cases retain separate executions, delivered goals and scores. The clarified comparison substitutes the nine changed-case results into the original 117-task set. Unchanged tasks share their original result. This is a paired comparison, **not a second full 117-task rerun** or a best-of-attempts score. Official evaluator conditions, fixture data and action budgets are unchanged by the wording patches. The paper-format task still fails after clarification; its failure is retained.

## GUI-only leaderboard comparison

The [official MobileWorld leaderboard](https://tongyi-mai.github.io/MobileWorld/) and its [source JSON](https://github.com/Tongyi-MAI/MobileWorld/blob/main/site/leaderboard.json), checked **2026-10-02**, list Qwen-UI-Agent at 82.1% as the highest published 50-step GUI-only result. ClickClick ranks **first in this GUI-only comparison**. The original-wording score of 90.60% also exceeds the published rows.

This comparison includes Agentic, General and Specialized categories, using their GUI-only scores rather than overall, user-interaction or MCP results. ClickClick is a system with skills and runtime adaptations. Its 95.73% result uses locally clarified instructions; other rows use their published protocols.

## Instruction clarifications / 指令澄清

These are disclosed local corrections and interpretations, not upstream-confirmed amendments. No patch supplies expected answers, selected items or expected counts. The exact delivered goals for both arms are in the [public score data](mobileworld-results-20261002.json), with synthetic login credentials omitted.

| Task | Issue / 问题 | Clarification / 澄清内容 | Original → clarified |
| --- | --- | --- | --- |
| `CheckDeduplicatedEventsTask` | Counting unit unspecified / 去重计数单位不明确 | Count recurring series once across the date range and multi-day events once / 同一重复事件系列在日期范围内计一次，跨日事件计一次 | Fail → Pass |
| `MattermostProjectStatusReportTask` | Three-day window lacks a reference date / 三天窗口缺少基准日期 | Use each update's completion/target date ±3 calendar days, not today / 以更新中的完成或目标日期为基准，前后三个自然日匹配里程碑 | Pass → Pass |
| `CartManagementTask` | Ambiguous deletion categories / “短袖T恤衬衫”范围歧义 | Delete any short-sleeved item, T-shirt or shirt; retain others / “短袖、T恤、衬衫”为并列条件，符合任一项删除 | Fail → Pass |
| `DownloadSendReceiptTask` | Goal filename differs from fixture/evaluator / 原题文件名与初始化文件、评分器不一致 | Correct `receipts.jpg` to `receipt.jpg` / 修正为单数文件名 | Fail → Pass |
| `SendFormsTask` | Existing emails can be later than the device date / 已有邮件晚于设备日期 | Include October 3 and all later existing emails, without a device-date upper cutoff / 包含 10 月 3 日及已有后续邮件，不以设备日期封顶 | Fail → Pass |
| `ReadQwen3PaperTask5` | Language names versus exact code/order scoring / 语言名称与精确代码、顺序判定不一致 | Return language-and-script codes in table order, comma separated / 按表格顺序返回语言与文字代码，以逗号分隔 | Fail → Fail |
| `InvoiceReceiptCopyAskUserTask` | Destination only in simulated-user information without an interaction tag / 目标目录仅在模拟用户信息中，未标注用户交互 | Supply `Documents/expense/invoice`; create if absent / 补充目标目录，不存在则创建 | Fail → Pass |
| `MastodonShareLocationTask` | Scoring requires a place name beyond the explicit link/photo request / 判定额外要求正文地点名 | Include the place name as post text alongside link and photo / 正文增加地点名称 | Fail → Pass |
| `MastodonServerInfoReportTask` | Official account information not explicit in goal / 官方账号信息未在任务中显式给出 | Append official synthetic account context; login stays GUI work / 补充官方合成测试账号信息，登录仍通过 GUI 完成；此项属于环境信息补充 | Pass → Pass |

The destination omission is also described in [upstream issue #55](https://github.com/Tongyi-MAI/MobileWorld/issues/55). Other local interpretations should not be described as confirmed upstream task defects. Execution mistakes remain failures even when the wording is ambiguous.

## Environment disclosure

Fixture readiness is checked before execution. TaoDian can receive one cold relaunch when it has not applied its official configuration; unresolved readiness errors block execution. `MastodonNewFilterTask` synchronizes the emulator with the live server clock before execution because the native date picker and server use different clocks for filter expiry. Other tasks keep their official clock policy.

`MastodonServerInfoReportTask` and `MastodonGetServerInfoTask` receive official synthetic owner-account information as model-visible environment context. Account switching and login remain agent GUI actions; no authenticated cookies are injected. The final original-wording arm includes the disclosed replacement executions for these two cases, with original goals and scoring conditions preserved. Earlier executions remain archived separately and are not silently relabeled. The ninth variant additionally appends the same account context to the report task's goal. It contributes no extra pass.

## Six complex successes / 六个复杂成功用例

| Task | Workflow | Variant | Recording |
| --- | --- | --- | --- |
| `MattermostProjectStatusReportTask` | Three teams → calendar milestones → risk email → escalation events → channel summary / 三团队状态与里程碑核对、风险邮件、升级事件、频道汇总 | Clarified | [Video](assets/demos/mobileworld/project-risk-matrix.mp4) |
| `MattermostResourceConflictResolutionTask` | Resource requests → conflict checks → bookings → requester notifications → report / 预订请求、冲突核对、创建预订、通知申请人、报告 | Original | [Video](assets/demos/mobileworld/resource-conflicts.mp4) |
| `ThanksgivingPrepTask` | Recipe website → ingredient email → date-relative shopping event / 网页食谱、食材邮件、按节日日期安排采购 | Original | [Video](assets/demos/mobileworld/thanksgiving-preparation.mp4) |
| `MastodonCreateMemoTask` | Hashtag lecture search → read time/location → event and one-day reminder / 话题讲座查找、读取时间地点、创建事件与提前一天提醒 | Original | [Video](assets/demos/mobileworld/lectures-to-calendar.mp4) |
| `LocalFileManagementTask2` | Date filter → ZIP archive → remove originals → email deletion list / 按日期筛选、压缩归档、删除原件、邮件留档 | Original | [Video](assets/demos/mobileworld/archive-old-files.mp4) |
| `PhotoManagementTask` | Calendar travel information → date/category filtering → location folders / 日历旅行信息、日期与类型筛选、地点文件夹分类 | Original | [Video](assets/demos/mobileworld/photos-by-travel-location.mp4) |

All six are score-1 successes within their recorded round budgets from this exact batch. Full videos preserve every captured frame and divide the original timing by three. GIF previews sample eight moments. Account names, addresses and messages shown are public synthetic benchmark fixtures. Public artifacts contain no raw databases, model conversations, host paths, device identifiers or real credentials. The [media manifest](assets/demos/mobileworld/manifest.json) records source/output SHA-256 digests, task/variant identity, actions, timing and exported steps.
