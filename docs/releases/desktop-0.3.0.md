# ClickClick 0.3.0

This release adds human-in-the-loop questions and confirmations, and strengthens Skills self-learning.

## Human-in-the-loop (HITL)

- Turn on **Ask me when needed (HITL)** in Console settings. It is off by default and applies to new tasks.
- Planner clarifies important unresolved intent; Executor can ask when execution reveals a necessary choice or authorization gap. Already supplied answers and permissions are reused.
- Answer one question or a group of up to three in Console or the foreground CLI. Assistant hosts can integrate a conversation-specific reply bridge, with automatic Console fallback when the origin disconnects.
- Pending questions and replies are saved. A matched answer resumes the original call, keeps consumed budgets and deadlines, and refreshes the device view before continuing. Versioned ownership prevents conflicting replies from two surfaces.
- MobileWorld interaction evaluation: **34/44 (77.27%)**, with GPT-5.6-Sol and Jev off. [Explore all 44 tasks and question/answer steps](https://lordrosenberg.github.io/ClickClick/mobileworld/?category=hitl#explore).

## Stronger Skills self-learning

- Improve reusable application knowledge from real task evidence and explicitly authorized device exploration.
- Give Learner and Reviewer better bounded recovery, history lookup, feedback and continuation support, reducing repeated or unfocused exploration.
- Keep independent review and selective validation before candidates enter the pending library. Multi-app skills and local adoption retain their evidence and review status.
- Learning remains opt-in. **Optimize my workflow** lets you choose the learning budget; generated candidates still require approval before becoming shared skills.

Choose the Windows x86_64, macOS Apple Silicon, or macOS Intel installer below. Upgrading retains model settings, task history and saved Skills. See the [installation guide](https://github.com/LordRosenberg/ClickClick/blob/main/docs/desktop-setup.zh-CN.md).

**macOS:** Packages are not Developer ID signed or Apple notarized. Gatekeeper may prevent opening them; source deployment is available in the installation guide.

---

本次正式版主要新增 **HITL 提问与确认**，并强化 **Skills 自学习能力**。

## HITL 提问与确认

- 在 Console 设置中开启「必要时向我提问或确认（HITL）」，默认关闭，对新任务生效。
- Planner 澄清重要的未确定意图；Executor 在执行中遇到必要选择或授权缺口时补充提问，复用已有回答和授权。
- Console 与前台 CLI 支持单题或一次最多三题；助手宿主可接入会话回复桥接，原入口断开后自动转到 Console。
- 问题和回答持久保存，匹配回答恢复原调用，保留已消耗预算与截止时间，继续操作前重新观察设备。回答入口版本校验避免多处回复冲突。
- MobileWorld 交互原题 **34/44（77.27%）**，GPT-5.6-Sol，Jev 关闭。[查看全部 44 题与独立问答步骤](https://lordrosenberg.github.io/ClickClick/mobileworld/?category=hitl#explore)。

## 强化 Skills 自学习

- 结合真实任务证据与明确授权的设备探索，提炼可复用的应用操作知识。
- 加强 Learner、Reviewer 的有界恢复、历史检索、反馈和连续探索，减少重复或偏离目标的探索。
- 候选进入待审库前保留独立审查和必要的局部验证，多应用候选与局部接纳均保留证据和审查状态。
- 自学习仍为可选功能，通过「优化我的工作流」明确学习预算，候选仍需用户审批后才能进入共享技能库。

请选择 Windows x86_64、macOS Apple Silicon 或 macOS Intel 安装器。升级保留模型配置、任务记录和已保存的 Skills。参见[安装指南](https://github.com/LordRosenberg/ClickClick/blob/main/docs/desktop-setup.zh-CN.md)。

**macOS：安装包未经过 Developer ID 签名和 Apple 公证，可能被 Gatekeeper 阻止打开。** 也可选择源码部署。
