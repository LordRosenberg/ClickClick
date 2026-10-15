# MobileWorld User-Int. / HITL — 2026-10-15

[English README](../README.md) · [中文首页](../README.zh-CN.md) · [Interactive results / 交互成绩](https://lordrosenberg.github.io/ClickClick/mobileworld/?category=hitl#explore)

**34/44 passed (77.27%); 10 failed; 0 unscored.** All 44 original non-MCP User-Int. tasks were evaluated with the official MobileWorld predicates, once per task, without clarified replacements or best-of selection. The fixed denominator is 44. GUI-only and MCP categories are separate.

**44 道交互原题通过 34 道（77.27%），失败 10 道，未评分 0。** 每题一次有效尝试，使用官方评分器，未替换题目措辞或挑选多次运行的最佳结果。纯 GUI 与 MCP 类别独立统计。

## Method / 方法

- Agent: ClickClick Planner + Executor, GPT-5.6-Sol, high reasoning; native UI trees, screenshots and scoped skills. Jev disabled.
- Interaction: the official MobileWorld user simulator, routed to GPT-5.6-Sol. Each `ask_user` and its matched answer resumes the originating role call. These are simulated benchmark answers, not private conversations with real users.
- Budget: 50 prediction rounds per task, including questions and terminal answers; local 2,400-second deadline; no separate model-call cap.
- Evidence: 44 continuous recordings, 941 prediction frames and 56 matched question/answer exchanges. Public trajectories retain Planner/Executor events and show each question with its answer as a distinct step. Authentication answers and private machine paths are omitted.
- Environment: Android API 34; one emulator; app/session, clock and fixture readiness checked before each task. Public fixture account context was supplied where needed; login remained a GUI action. The 44 task definitions match official reference `aa2ea47052dd15729bfca1f27fa6aca4630607a9`; the deployed archive base was `e41d1478e252325c513003d3d191b4c164b4af2c`, with a bounded Mattermost readiness wait.

## Comparison / 排名口径

**#1 in our User-Int. comparison / 交互类成绩对比第 1。** The [published leaderboard](https://tongyi-mai.github.io/MobileWorld/) was checked on 2026-10-16: its highest numeric 50-step User-Int. row was 65.9%, below 77.27%. This is a comparison of our local evaluation with published results, not a claim that ClickClick has been accepted onto the official leaderboard. Agent configurations and repeat counts differ across entries.

## Results and limits / 成绩与边界

The public gallery includes all 44 outcomes, including all 10 failures. A score is the official predicate outcome; it is not proof of full semantic completion or a guarantee against missed questions. Some passing cases have unresolved differences between the agent's completion report and the predicate result. Neither those cases nor valid failures were regraded for this publication. The runtime preserves pending questions and matched continuation reliably; the model still decides when clarification is necessary. A separate semantic guardrail for missed questions is not part of this release.

公开页面保留全部 44 题和 10 个失败。成绩表示官方评分器的判定，不代表所有请求效果均已实现，也不能保证模型不会漏问；部分通过题仍存在模型完成报告与评分结果的差异，未为发布修改评分。HITL 负责可靠等待和恢复，提问时机仍由模型判断，本次未增加漏问后的独立语义拦截。

[Public per-task data / 公开逐题数据](mobileworld-hitl-results-20261015.json) · [HITL usage and design / 使用与设计](hitl.md)
