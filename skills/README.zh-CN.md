# ClickClick Skills

[English](README.md) · [中文首页](../README.zh-CN.md)

Skill 保存可复用的应用操作知识。用户仍然用自然语言描述目标；Agent 根据任务选择适用内容，不要求用户指定 Skill ID 或按照 Skill 的结构提问。

<a id="when-to-add-a-skill"></a>
## 什么时候需要补充 Skill？

先使用仓库已有技能运行一个有代表性的任务。若多次执行暴露了稳定、可解释的应用知识缺口，再补充或修订相关 Skill。可以优先考虑以下场景：

| 场景 | 值得写入的知识 |
| --- | --- |
| 应用有特殊交互 | 隐藏入口、编辑与保存的区别、控件含义，以及适用的应用或系统版本。 |
| 跨应用转录容易丢信息 | 来源中哪些字段必须保留、目标字段如何对应、何时回查来源。具体食谱或费用数据仍由本次任务提供。 |
| 长列表或相似记录容易误处理 | 怎样区分记录、记住处理进度、判断搜索或遍历何时结束。可先复用通用列表遍历技能。 |
| 多字段任务容易只做对一部分 | 字段依赖关系、提交后检查哪些状态，以及如何确认应用已保存。 |
| 同一应用操作会反复使用 | 已验证的操作方法、已知失败模式和有明确触发条件的替代路径。 |

例如，若任务经常在“填写完成”后直接结束，而应用还需要单独保存，就将保存方式与保存后的检查方法写入应用 Skill。一次具体任务中的标题、金额和日期属于任务数据，不应固化成通用规则。

这里的 `workflow` 是一类任务的操作指南，不是要求用户照抄的提示词，也不是固定点击脚本。步骤应围绕目标和可观察状态编写，允许 Agent 根据当前页面调整。普通指导默认写成建议；将规则设为硬约束需要明确适用范围和证据。

## 如何判断补充是否有效？

1. 在 Console 查看失败时的模型输入、active skills、动作回执和最终页面，确认问题确实来自缺少或错误的操作知识。若现有技能未被交付，应先检查其描述、应用和设备范围。
2. 将最小的可复用经验加入应用 core 或已有任务 Skill；新增内容与既有知识重复时，优先修订已有内容。
3. 用不同数据和有代表性的初始页面验证，并检查最终保存状态。比较改动前后的表现时固定模型、应用版本和预算，保留失败尝试。

Skill 有助于减少已知错误，不能保证每次成功。模型服务超时、设备断连、截图过期或工具实现错误，需要修复对应组件；增加提示文字不能替代这些修复。

文件系统中的 skills 是 Agent 先验知识在运行时的**唯一权威来源**，不存在 SQLite／数据库 skill 存储。

## 运行时交付

Planner 接收所有激活 workflow 的精简卡片。任务别名和精确前台身份用于优先排序；指令未提应用名时不会隐藏能力。对每个执行子目标，Planner 可选择一个目标应用及最多两个 workflow ID。Executor 与 Reviewer 接收该应用 core 和所选、按角色筛选的正文。前台身份仍是独立观测证据，无关的起始应用或覆盖层不会替换交接目标。

Planner 优先提供应用名或别名；runtime 在接受新计划、绑定 skill 前解析实际安装包，也校验显式包名，未安装的猜测包名不会建立阶段的 skill 范围。

精确观测到的前台应用还会补充自身 core，即使目标应用不同或历史计划的包名猜错。它只提供当前控件指导，不改写目标、不自动选择前台 workflow。离开前台后退出活跃交付；前台与目标相同时去重。

Skill ID 全局唯一。加载时发现重复激活 ID 会拒绝继续，而不是静默隐藏前台工作流。交付通过通用 active-skill 元数据记录：ID、版本、内容哈希、范围、激活来源及规则类别。

Executor 不能加载或替换 skills。Planner 可从索引按精确 ID 加载 generic skill，但选择 workflow 使用已提供卡片，不依赖自由文本搜索。引用资源不会自动加载。

每个阶段最多选择四个 generic/workflow ID，其中应用 workflow 最多两个。跨屏记录列表可将 `adaptive-list-traversal` 与相应 App workflow 一起选择：通用技能维护按锚点调整滚动幅度和遍历覆盖规则，App 技能保留排序、字段比较及控件知识。App 正文提到通用技能不代表它已自动加载，需验证实际阶段交付。

## 界面归属与系统绑定

每份 skill 声明 `interface_scope`：

- `generic`：不依赖具体界面的通用方法，跨 App、跨系统共享。通用权限判断属于此类，固定权限页操作流程不属于此类。
- `app`：三方 App 自己实现的功能，按精确包名匹配，默认跨系统共享。预装不等于系统专属，例如 Chrome 网页内容仍属于 App。
- `system`：系统 App 或系统提供的权限页、文件/照片选择器、分享面板。即使从三方 App 进入，也必须绑定非空 `device_profiles`；缺失则拒绝加载。

`device_profiles` 对三类都有效，可保留已有系统例外。默认共享不代表所有 App 版本均验证过；已知版本条件仍保留在正文，当前运行时不校验 App 版本。相同包名只用于匹配 App，不会让不同包名的克隆应用自动继承 skill。

混合流程应拆开 App 内操作和具体系统界面步骤。仅要求按当前可见弹窗判断的通用方法不必拆成多个系统版本。DocumentsUI core 当前限定到已验证的 `androidworld_api33`，不直接推断兼容 API 34。

profile ID 来自 `shared/app_alias_profiles.json`，依据设备指纹匹配。目录、精确 ID 读取和角色正文统一过滤；设备未知时仅提供没有 profile 限制的 skill，切换设备会清除旧范围。交付轨迹记录界面归属和声明/实际匹配的 profile。创建和更新 API 接受这些字段，写文件前验证。

兼容旧文件时，已登记的系统组件包名默认 `system`，其他 App 默认 `app`，无 App 默认 `generic`；不按包名前缀或是否预装猜测。新 skill 应明确标记归属，尤其是新系统组件及从三方 App 进入的系统界面。

## 目录结构

```
skills/
  generic/<name>/SKILL.md   # cross-app priors
  apps/<app_id>/core/SKILL.md
  apps/<app_id>/workflows/<workflow-id>/SKILL.md
  apps/<app_id>/workflows/<workflow-id>/references/  # optional, not auto-loaded
  _pending/                 # Learner patches — NOT in the hot index
```

`generic` 保存跨应用先验；`references` 为可选资源，不会自动加载；`_pending` 保存 Learner 补丁，不进入活动索引。

## 文件格式

使用 YAML frontmatter 加 Markdown 正文（Hermes／OpenClaw 风格）。以下字段、章节名与示例保持原样，避免翻译改变运行时识别方式：

```markdown
---
name: bilibili-search-open-video
description: B站入口、搜索打开视频与常见遮罩。用户提到B站时使用。
version: 0.1.0
app: tv.danmaku.bili
kind: workflow
capability: search_open_video
tags: [video, search]
# 仅限 App core/workflow；配方只能使用封闭且经过校验的模板。
verified_actions:
  - id: example.inspect_and_return
    template: tap_capture_key
    key: back
    purpose: Open the target, preserve detail evidence, and return.
---

# Title

## Procedure
...

## Verification
...

## Pitfalls
...

## Constraints
- Never do X | scope: surface/workflow | evidence: stable source or trace refs

## Hints
- Prefer Y when it is visible

## Fallbacks
- Use Z | trigger: primary path is absent or failed

## Anti-patterns
- Repeating W caused a demonstrated loop

## Executor notes   # optional — Planner/Reviewer do not see this
...

## Decision notes   # optional — Executor does not see this; Planner/Reviewer do
...
```

### 编写约定

- `app_core` 保持精简，只包含稳定且适用于整个应用的指引。每个稳定用户意图放入独立 `workflow` 目录。
- 每个 workflow 声明 `app`、`capability`、`description` 和 `version`，并包含非空 `## Procedure` 与 `## Verification`。不添加检索触发器或页面清单，模型从完整应用目录选择。
- `verified_actions` 是 App 范围内的执行授权，不是检索提示或自由形式宏语言。只有 `app_core` 或 workflow 可以声明封闭且已识别的配方；仅当该 Skill 已激活且其 `app` 与当前前台包名相符时，运行时才允许调用该精确 ID。generic Skill 不能授予此权限，内部配方字段不会进入模型可见正文。
- Procedure 步骤是指引，不自动成为硬约束。
- 默认使用共享正文。仅在事实明确只对单一角色有用时使用角色注释，例如 Executor 的 `need_image` 提示。`Executor notes` 只给 Executor；`Decision notes` 给 Planner／Reviewer。
- 保守分类规则：**Constraint** 是有明确范围的硬不变量，每项需有 `scope` 和 `evidence`，只用于违背后不可能成立、无效或不安全的情况；**Hint** 是成功率／效率建议，不确定文字默认归此类；**Fallback** 是带显式 `trigger` 的替代路径；**Anti-pattern** 是已有证据的循环、失败或有害动作。
- 当前观测可覆盖提示，不能覆盖适用约束。约束与子目标冲突时，Executor 返回 `replan`。
- `_pending/` 下 Learner 输出须显式批准后才成为正式内容。审核需选择合并到 app core、合并到现有 workflow、新建 workflow 或保留为未激活候选。Learner 编写的约束还需要狭窄范围、证据和操作人员显式批准。

## 编写途径

1. 为重点应用手工编写教师 skills。
2. SkillLearner（任务结束后／Console “learn from this task”）→ `_pending/` → 人工批准。
