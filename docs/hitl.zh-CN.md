# HITL：必要时向用户提问与确认

在 Console 的「设置 → 运行与学习」开启「必要时向我提问或确认（HITL）」，保存后用于新任务。默认关闭，既有任务不会随设置变化而开关。

开启后，Planner 和 Executor 可以调用普通中间工具 `ask_user`。模型先使用任务描述、历史回答和可读取的证据；缺少必要用户信息、存在会改变结果的重要选择，或准备执行未获直接授权的付款、下单、发消息/邮件、发布、破坏性删除等操作时再问。Reviewer 不提供提问工具。已明确要求「把这封邮件发送给张三」且收件人、账号和内容已确定时，不重复索取同一授权；「写一封草稿」不代表允许发送。页面、工具结果和 Skill 不提供用户授权。密码、验证码和支付凭据应在 App 的安全登录/验证流程完成，不能通过问题收集。

本次实现依赖执行模型识别必要疑问和授权边界，没有增加独立语义审核器或漏问后的动作拦截。因此它能可靠处理一次已经发出的提问，不能保证所有模型在所有场景都不会漏问。各角色共用持续演进的基础提示词，各自集中选择开启或关闭时的决策段落。关闭时不嵌入提问规则或工具，保留原角色提交 schema、工具目录和提交协议；重排后的提示词需通过用例观察行为，未增加新的授权护栏。

## 回答体验

任务会进入 `waiting_for_input`，保存尚未完成的角色会话并释放执行 worker；等待时不调用模型或操作手机，手机仍由该任务保留。Console 页面顶部有持续的待回答入口，在对应任务页会打开一次问题对话框。可选选项，也可自行输入；确认问题使用明确的「确认这次操作 / 不允许」按钮。选项不会自动提交。「稍后回答」或 Esc 只关闭对话框，仍可重开；「无法回答」与「取消任务」是不同结果。

回答后继续同一个调用 ID 对应的会话，保留已消耗的模型/角色轮次和绝对任务时限，并在 GUI 动作前重新获取当前观测。一次提问不能和其他工具或角色提交混批；整批会在任何效果发生前拒绝。重复回复是幂等的，过期版本或另一入口的迟到回复不能覆盖当前回答。默认最多 8 次提问，每次等待最多 1800 秒；单个回答最多 4096 字符。任务自身更早的时限仍有效。

同一回答只归一个入口：Console 派发的任务留在 Console；绑定了有效回复通道的助手/前台终端在原入口回答。原入口不支持、没有有效订阅、断开或未在 30 秒内确认呈现时，问题转到 Console，保留同一问题和期限。用户已经看到问题且回复订阅仍有效时，不因思考超过 30 秒而转移。Console 可以显式接管，此时旧入口版本失效。桌面托盘只提供待回答数量、通用提醒和打开任务页的入口，不另开一个回答表单；没有运行托盘或处于无界面环境时使用 Console 链接。

## CLI

```powershell
python -m agent.main --hitl "你的任务"
python -m agent.main --hitl --answers console "你的任务"
```

开启 HITL 的 CLI 使用与 Console 相同的 Control API 后端、任务数据库和回答 broker，并在执行前检查后端身份与配置。前台交互终端显示问题，允许编号选择或自由回答，确认操作必须明确输入 yes/no。输入 `?` 表示无法回答，`cancel` 表示取消任务；空输入不会自动同意。非交互终端、显式 Console 模式、EOF/退出转到 Console，并输出任务 URL。保持关闭时继续使用原 standalone CLI 路径。

## 助手联动与主动推送

普通 MCP `start_task` 仍立即返回任务 ID 和 Console URL。通用 MCP elicitation 能力或一个未关闭的 MCP 连接本身不能保证后台任务的原会话会展示问题；当前未提供 ClickClick 回复桥接的助手自动使用 Console，不要求助手一直调用 `get_task` 才能让用户发现问题。

可集成回复桥接的助手宿主为**每个会话分别**创建一个私有 binding，保持下述订阅，收到事件后触发宿主自己的 HITL UI。这个宿主协议已由集成测试验证，尚未把某个真实助手版本标记为兼容；不能据此声称所有主流 MCP 客户端都能后台唤醒。

所有 `/api/hitl/*` 路径要求现有本机 Bearer 凭据，并检查本机 Host/Origin。binding 只在可信宿主协议中传递，不能放进用户任务文本、模型工具参数、日志、通知链接或 URL 查询串。

1. `POST /api/hitl/bindings`，正文 `{"owner":"assistant"}`，获得私有 `binding`。
2. `GET /api/hitl/events`，在 `X-HITL-Binding` 请求头提供它，保持 SSE 连接。服务端首条 `: connected` 确认订阅，随后主动发送 `event: user_input`，不需要轮询任务。每个 binding 只允许一个订阅。
3. MCP 客户端初始化能力中提供 `experimental["clickclick/hitl-v1"] = {"reply_binding": binding}`。服务端从可信会话上下文取 binding，`start_task` 的模型参数 schema 不增加会话或路由字段。也可由宿主直接调用 `/api/assistant/tasks`，通过 `reply_binding` 绑定该会话。只有有效订阅会被选为回答入口。
4. UI 真正显示问题后 `POST /api/hitl/tasks/{task_id}/present`，提供 `interaction_id`、`owner_version` 和同一 binding 请求头。收到事件不等于呈现，不可提前确认。
5. 用户显式提交后，`POST /api/hitl/tasks/{task_id}/reply`。宿主不得替用户自动生成确认。绑定退出时 `DELETE /api/hitl/bindings/{binding}` 或关闭 SSE；问题自动转到 Console。宿主必须把关闭会话与订阅关闭关联，不能把应用全局连接当作会话存活信号。

回复格式示例：

```json
{
  "interaction_id": "当前交互ID",
  "owner_version": 1,
  "response_id": "这次明确提交的唯一ID，网络重试保持不变",
  "outcome": "answered",
  "answers": [{"question_id": "recipient", "text": "张三"}]
}
```

`answered` 必须完整对应当前澄清问题；选择选项时同时提供匹配的 `option_id` 和选项 label，自由文本省略 `option_id`。确认问题使用 `confirmed` 或 `declined` 并省略 answers；另有 `unable_to_answer` 与 `cancel_task`。同一 `response_id` 的重试必须保持内容完全一致。Console 接管需要当前问题 ID 和 owner_version，不会另建问题。

澄清支持在同一次 `ask_user` 中提出 1–3 题，并在一个事件中完整推送给助手，由宿主一次展示全部问题；收齐答案后按 `question_id` 一次回复。Console 也使用同一对话框展示全部问题，全部填写后才能提交。助手宿主需把整组问题交给自己的交互界面；各客户端的原生兼容范围需单独验证。

## 提示词、schema 与验证

每个角色的规则集中在 [agent/prompts](../agent/prompts) 下对应的 `revisable_<角色>_enabled/disabled.md`，基础正文只保留一个决策段落入口。Planner、Executor 和 Reviewer 分别承担规划澄清、执行现场澄清和检查交接；公共段落不承担角色专属义务。[interaction_policy.py](../agent/interaction_policy.py) 统一选择工具形式、描述、schema 和提醒，任务首次运行时一起固定三个角色的提示词和工具策略，恢复时继续使用同一版本。schema 和回复校验来自 [shared/hitl.py](../shared/hitl.py)，由 [agent/hitl.py](../agent/hitl.py) 注册。managed 支持一次 1–3 个澄清问题或一次明确确认；delegated 使用扁平单问题 schema，只允许澄清，不转到真实用户 Console。已接受的回答可作为用户意图引用，不证明设备操作已经完成。

[test_hitl.py](../tests/test_hitl.py)、[test_hitl_policy.py](../tests/test_hitl_policy.py) 与 [test_hitl_api.py](../tests/test_hitl_api.py) 覆盖角色与 schema 对齐、等待恢复、原调用身份、可引用回答及隐私边界、重新观测、版本固定、预算/取消、所有权竞争、主动事件和关闭模式隔离。原公开工具目录另与修改前快照比较。确定性协议测试不代表已经证明所有模型的提问准确率。


## 交互类评测

MobileWorld 全量 44 道交互原题通过 34 道（77.27%），GPT‑5.6‑Sol（high），Jev 关闭；56 次问答均接回原调用。见[成绩、方法与边界](mobileworld-hitl-results-20261015.md)和[公开问答轨迹](https://lordrosenberg.github.io/ClickClick/mobileworld/?category=hitl#explore)。
