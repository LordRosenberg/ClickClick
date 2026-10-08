<div align="center">

# ClickClick

**让 AI 在 Android 上完成真正的工作。**

跨应用任务 · PC 助手接入 · 执行过程可查看

[English](README.md) · [评测成绩](https://lordrosenberg.github.io/ClickClick/) · [效果演示](#效果演示) · [快速开始](#通过-mcp-接入-pc-端-ai-助手) · [源码部署](#部署) · [文档](#文档导航)

[![MobileWorld GUI-only](https://img.shields.io/badge/MobileWorld%20GUI--only-95.73%25%20%28112%2F117%29-14866d)](https://lordrosenberg.github.io/ClickClick/mobileworld/)
[![AndroidWorld](https://img.shields.io/badge/AndroidWorld-99.14%25%20%28115%2F116%29-14866d)](https://lordrosenberg.github.io/ClickClick/androidworld/)
[![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/License-Apache--2.0-green)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Android%208.0%2B-3DDC84?logo=android&logoColor=white)](docs/deployment.zh-CN.md)

</div>

ClickClick 是一个开源 Android Agent 平台。用自然语言描述目标，它就能跨应用搬运信息、填写复杂表单、管理记录、配置日程，并规划步骤、操作设备和检查结果。Web Console 让你随时查看执行进度，深入检查每一次决策。

通过 **MCP** 接入 PC 端 AI 助手后，你可以直接在助手对话中交办手机任务、查看进度，或暂停和恢复执行。

## MobileWorld 纯 GUI：95.73%，成绩对比第 1

最新测试原题成绩为 **106/117（90.60%）**，加入 7 道澄清题补充后的对照成绩为 **112/117（95.73%）**。使用 `chatgpt/gpt-5.6-sol`（high）、Android API 34，每题最多 50 个 MobileWorld 回合、2400 秒；117 道原题均有官方数值评分。运行时结合可修订规划、持久记忆、作用域技能与 Jev 核验。

澄清对照采用 6 道措辞澄清及 1 道官方账号信息补充，其余任务采用原题结果。两项成绩均高于 2026-10-07 核对的公开 50 回合纯 GUI 榜单各项。

[逐题成绩、截图与执行轨迹 →](https://lordrosenberg.github.io/ClickClick/mobileworld/) · [澄清明细与评测方法](docs/mobileworld-results-20261007.md)

## AndroidWorld：99.14%，按成功率档位并列第 2

使用 `chatgpt/gpt-5.6-sol`（high）在 Android API 33 上完成评测，**115/116 个任务通过官方评分**。

[成绩与执行轨迹 →](https://lordrosenberg.github.io/ClickClick/androidworld/) · [评测方法与配置](docs/androidworld-results-20260921.md)


按 2026-10-02 的[公开榜单](https://docs.google.com/spreadsheets/d/1cchzP9dlTZ3WXQTfYNhh3avxoLipqHN75v1Tb86uhHo/edit?gid=0)及其一位小数精度，与 Artemis 同为 99.1%，按成功率档位并列第 2；100% 为第 1 档。

## 效果演示

### MobileWorld 演示

六个跨应用成功任务录屏。点击预览，观看 **3 倍速完整视频**。

| 项目风险矩阵 | 资源预订冲突 | 网页资料转采购计划 |
| --- | --- | --- |
| [![项目风险矩阵](docs/assets/demos/mobileworld/project-risk-matrix.gif)](docs/assets/demos/mobileworld/project-risk-matrix.mp4) | [![资源预订冲突](docs/assets/demos/mobileworld/resource-conflicts.gif)](docs/assets/demos/mobileworld/resource-conflicts.mp4) | [![网页资料转采购计划](docs/assets/demos/mobileworld/thanksgiving-preparation.gif)](docs/assets/demos/mobileworld/thanksgiving-preparation.mp4) |

| 讲座转日历 | 文件归档与邮件留档 | 按旅行地点整理照片 |
| --- | --- | --- |
| [![讲座转日历](docs/assets/demos/mobileworld/lectures-to-calendar.gif)](docs/assets/demos/mobileworld/lectures-to-calendar.mp4) | [![文件归档与邮件留档](docs/assets/demos/mobileworld/archive-old-files.gif)](docs/assets/demos/mobileworld/archive-old-files.mp4) | [![按旅行地点整理照片](docs/assets/demos/mobileworld/photos-by-travel-location.gif)](docs/assets/demos/mobileworld/photos-by-travel-location.mp4) |

[任务指令与录屏说明](docs/demos.md#mobileworld-recordings--mobileworld-录屏) · [最新截图与执行轨迹](https://lordrosenberg.github.io/ClickClick/mobileworld/)

### AndroidWorld 演示


真实成功任务录屏。点击预览，观看 **3 倍速完整视频**。

| 笔记 → 食谱 | 图片 → 费用记录 | 重复日历事件 |
| --- | --- | --- |
| [![从 Markor 向 Broccoli 转录食谱](docs/assets/demos/notes-to-recipes.gif)](docs/assets/demos/notes-to-recipes.mp4) | [![从图片读取费用并录入 Pro Expense](docs/assets/demos/image-to-expenses.gif)](docs/assets/demos/image-to-expenses.mp4) | [![创建重复日历事件](docs/assets/demos/recurring-calendar-event.gif)](docs/assets/demos/recurring-calendar-event.mp4) |
| 读取笔记，创建 **3 份食谱**，保留食材、做法及其他字段。 | 读取来源图片，录入 **3 条费用**的金额、类别和备注。 | 设置日期、开始时间、**45 分钟时长**、描述和每日重复规则。 |

| 费用去重 | 按顺序创建歌单 | 每周运动时长统计 |
| --- | --- | --- |
| [![辨别重复费用并保留不同记录](docs/assets/demos/deduplicate-expenses.gif)](docs/assets/demos/deduplicate-expenses.mp4) | [![查找歌曲并按指定顺序创建歌单](docs/assets/demos/ordered-playlist.gif)](docs/assets/demos/ordered-playlist.mp4) | [![筛选本周游泳活动并统计总时长](docs/assets/demos/weekly-activity-summary.gif)](docs/assets/demos/weekly-activity-summary.mp4) |
| 检查长列表中的费用，删除**完全重复的记录**，每种独立费用保留一条。 | 创建指定名称的歌单，查找 **2 首歌曲**，按要求排序并核对。 | 按**本周与游泳类别**定位活动，检查时长并给出分钟数汇总。 |

[任务指令与录屏说明](docs/demos.md)

## 为复杂任务而构建

- **自主规划，随执行调整。** 将目标拆成有意义的阶段，根据实际页面修订后续路线，延续已完成的工作；需要独立判断时调用 Reviewer。
- **跨应用记住关键信息。** 版本化笔记与短保留正文独立于结构化历史摘要保存，摘要逐条引用持久来源，近期完整步骤保持可见。需要细节时，可回读笔记、早先的截图和原始记录。[记忆与上下文管理](docs/architecture.zh-CN.md#上下文记忆与技能)
- **可靠操作真实界面。** 结合截图与无障碍结构定位控件，将支持的点击绑定到被观察的原生节点，检查文本输入，并根据动作反馈决定下一步。
- **把应用经验变成可复用技能。** 无需重新训练模型即可扩展应用知识。从任务记录中提炼流程和避坑点，经独立审查与必要验证后进入待审库，支持多应用候选及局部验收。[Skills 自进化](docs/skill-evolution.zh-CN.md)。应用自身的界面经验可跨设备共享，系统界面知识按设备配置匹配，通用技能可跨应用复用；受支持的复合动作可完成详情读取和返回，同时保留中间证据。
- **自由选择模型与设备。** 为规划和执行配置模型，连接本地真机或模拟器，也可通过远程 Driver 管理设备。

Agent Harness 将 **可修订规划、持久记忆、作用域技能和设备反馈** 组织成完整执行流程。[技术概览](docs/reliability-design.zh-CN.md)

## 看见并理解每次执行

Console 将已保存的观测截图与 Agent 执行历史放在同一个工作区。

- **跟随进度：** 查看最新记录的观测截图、当前阶段和流式模型响应。
- **浏览历史任务：** 分页查看任务概览，筛选失败任务，按需打开执行详情。
- **检查决策：** 打开 Timeline 调用，查看实际模型输入、返回决策、激活技能、工具调用和关联截图。
- **定位问题：** 沿动作追查目标、回执和结果观测，回看所选步骤或模型轮次的历史画面。截图不会重新采集手机。
- **分析开销：** 查看任务与模型耗时、调用次数，以及提供方返回的输入、输出和缓存用量。

[Console 与可观测性指南 →](docs/observability.zh-CN.md)

## 核心架构

![ClickClick 核心架构](docs/assets/clickclick-architecture.svg)

默认循环为 **Planner → Executor → 观测 → 继续或重规划**，Reviewer 按需介入。Harness 管理上下文、技能与执行，持久 Session 保存任务证据，Android 工具提供动作和观测；Console 展示执行过程，外部评测器检查任务结果。

Harness 分别组装原始任务、当前阶段、新鲜观测、结构化摘要、近期完整步骤与短保留笔记。观测包携带像素、UI 结构、身份和几何；原生细节裁剪／分片用于阅读，动作仍绑定新鲜全局观测。

[架构与模块接口](docs/architecture.zh-CN.md) · [设计取舍](docs/design-decisions.zh-CN.md)

## 通过 MCP 接入 PC 端 AI 助手

把 ClickClick 接入支持 MCP 的 PC 端 AI 助手，在对话中交办手机任务、查询进度，也可以随时暂停、恢复或取消。

### 推荐安装方式：让 AI 助手安装并连接

如果你的助手能在电脑上执行命令，把下面这段话发给它，让它代办下载、安装、手机连接和 MCP 设置：

> 请帮我在这台电脑上安装并连接 ClickClick（https://github.com/LordRosenberg/ClickClick）。按安装指南识别 Windows/macOS 和 CPU 架构，从官方 Releases 下载匹配的安装器并核对该发布的 SHA-256；没有匹配安装器时，按源码部署指南处理，不把源码压缩包当安装器。安装后检查退出状态及后台 identity。打开本机设置页，让我选择订阅登录或 API，协助官方授权；API Key 由我在页面填写，不放进聊天。问我用 Wi-Fi、USB 还是已有模拟器，代办电脑侧连接并具体指导手机调试与权限确认。在设置页的“让助手添加”入口取得包含完整本机配置文件路径的接入提示词，读取该 stdio 配置，并用你自己的官方 MCP 管理命令或配置入口注册 clickclick，保留已有设置。若通过安装版 CLI 导出，必须指定 --client generic --transport stdio --output 和一个明确的本机文件完整路径，再读取该文件；不要调用 ClickClick register 代替你的注册步骤。能在当前会话加载 MCP 就调用 get_status，检查模型配置和手机初始化，分享 Console 链接。若需我重启客户端或新开会话，请报告“注册完成，待验证”，给出具体操作，并让我在新会话发送：“请调用 ClickClick 的 get_status，检查连接、模型配置和手机初始化，说明是否就绪及未完成事项。”不要把注册或 ADB 在线当成验证成功；get_status 不进行模型请求或手机任务测试。只有我明确要求时才执行实际手机测试任务。

你只需在设置页选择 Codex 订阅登录或 API、完成模型和手机授权，并确认助手操作权限。[详细安装指南](docs/desktop-setup.zh-CN.md#两种首次安装入口)

### 也可以手动安装

1. **安装 ClickClick。** 从 [GitHub Releases](https://github.com/LordRosenberg/ClickClick/releases) 选择适合你 Windows 或 macOS 电脑的安装包。没有对应安装包时，可按[源码部署指南](docs/deployment.zh-CN.md)安装。
2. **配置模型，连接手机。** 打开设置页，选择 Codex 订阅登录或配置 API，通过 Wi-Fi 或 USB 连接 Android 手机，也可使用已有模拟器。按页面提示在手机上开启调试并确认权限。
3. **接入助手。** 在设置页复制“助手接入提示词”发给助手；也可以展开“手动添加”，按所用助手执行命令或添加配置。如需重开会话，使用页面单独提供的连接验证指令。

### 开始使用

连接就绪后，在助手对话中描述你要完成的事情，例如：

> 用 ClickClick 把这篇笔记里的三份食谱录入食谱 App，保留食材和做法，核对保存结果，并告诉我是否完成。

执行中可以继续问“现在进展如何？”或说“暂停这个任务”。让助手提供 Console 链接，即可查看执行步骤和保存的截图。运行时请保持电脑与手机在线。

[安装、手机连接与助手配置](docs/desktop-setup.zh-CN.md) · [MCP 接入指南](docs/local-mcp.zh-CN.md)

安装后可从桌面或应用程序入口启动 ClickClick，通过托盘图标打开控制台、启动/停止后台和设置开机启动。有任务运行时，关闭前可选择暂停或取消；关闭浏览器不会停止后台。

## 部署

以下适用于从源码安装；使用桌面安装器时无需执行这些步骤。需要 Python 3.12、Node.js/npm、Android SDK Platform-Tools，以及支持图像和工具调用的模型服务。先从本机连接的设备开始；远程部署见[部署指南](docs/deployment.zh-CN.md)。

### 1. 安装服务并配置模型

在仓库根目录执行：

```bash
# Linux / macOS
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[decode]"
cp .env.example .env
```

```powershell
# Windows PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[decode]"
Copy-Item .env.example .env
```

在 `.env` 中配置模型与凭据，例如使用兼容 OpenAI 的 API：

```dotenv
CLICKCLICK_DEFAULT_MODEL=openai/YOUR_MODEL_ID
CLICKCLICK_MODELS_JSON={"openai/YOUR_MODEL_ID":{"provider":"openai","base_url":"https://YOUR_ENDPOINT/v1","api_key":"YOUR_API_KEY","max_tokens":4096,"reasoning_supported":false}}
CLICKCLICK_DRIVER_URL=
CLICKCLICK_DRIVER_URLS_JSON=
CLICKCLICK_USE_FIXTURE_DRIVER=false
```

其他提供方、登录接入及推理参数见[模型配置](docs/deployment.zh-CN.md#model-routing)。

### 2. 连接设备

真机开启 USB 调试、连接电脑并接受设备上的授权提示；模拟器先启动并解锁。执行 `adb devices`，确认目标序列号后显示 `device`。若显示 `unauthorized`，在手机上完成授权。

### 3. 启动平台，等待自动初始化

```bash
npm --prefix web ci
npm --prefix web run build
clickclick-api
```

ClickClick 自动检测设备并部署 Accessibility Collector。保持设备解锁，按 Android 提示允许安装、调试或无障碍权限。如果设备尚未安装 ADBKeyboard，请按[输入法配置](docs/deployment.zh-CN.md#input-method-apk)提供 APK，以启用文本输入。

远程设备、Collector 安装与连接排查见[部署指南](docs/deployment.zh-CN.md)。

### 4. 运行首次任务并检查效果

打开 [Console](http://127.0.0.1:8080)，在任务创建区选择 decision model、executor model 和一个空闲设备，输入：

> 打开系统设置，在搜索框输入 Wi-Fi，查看无线网络相关设置并报告当前状态，不修改任何开关。

点击 **submit**，在 Console 中跟随执行进度。选择 Timeline 中的调用可查看模型输入、工具和保存的观测截图；开启“跟随最新步骤”查看新记录，点击模型图片查看该轮输入画面。

## 用自然语言下达任务

直接在 Console 中描述你想完成的事，例如：“把 Markor 里这篇笔记中的食谱添加到 Broccoli。”Agent 会根据目标和实际页面规划步骤、操作应用。无需固定提示词格式，也无需预先编排操作流程。

[任务示例](docs/task-examples.zh-CN.md)展示可以交给 Agent 的任务。示例中的应用、措辞和数据都可以按需替换；希望补充特定应用的操作经验时，可参考[技能指南](skills/README.zh-CN.md)，这是可选扩展。

也可以在任务详情点击 **“优化我的工作流”**，授权本次学习预算和设备探索；在 **技能 → 待审 Pending** 检查候选、证据与独立审查，再决定逐文件批准。普通任务结束不会自动启动学习，候选不会自动发布。

### 什么时候值得补充 Skill？

普通任务可以直接用自然语言提交，Agent 会使用仓库已有的适用技能。以下情况值得补充或改进 Skill，以提高同类任务的成功率：

- **应用操作不直观：** 特殊保存方式、隐藏入口、容易混淆的控件，或特定系统版本的交互差异。
- **同类任务反复踩坑：** 跨应用转录丢字段、长列表漏项、同名记录误判，或填完后没有真正保存。
- **需要稳定复用操作与核验经验：** 将已经验证的方法沉淀下来，让后续任务知道如何操作、如何检查结果。

本次任务的目标和具体数据写在指令里，可复用的应用经验写入 Skill。Skill 能减少已知错误，不能保证每次成功；可在 Console 中检查实际加载内容和执行结果。[如何编写和验证 Skill →](skills/README.zh-CN.md#when-to-add-a-skill)

## 评测与复现

MobileWorld 最新配置与成绩见[评测报告](docs/mobileworld-results-20261007.md)。另有[固定配置运行指南](docs/mobileworld-reproduction.zh-CN.md)，其准备与执行参数以该指南为准。



准备好模型与 AndroidWorld 模拟器后，可一键运行公开的 116 个任务实例：

```bash
python -m evaluation.androidworld.reproduce --install
```

[完整准备、运行与恢复指南](evaluation/androidworld/README.md)。

在 [AndroidWorld 成绩页](https://lordrosenberg.github.io/ClickClick/androidworld/)查看逐项评分与执行轨迹。[评测报告](docs/androidworld-results-20260921.md)说明 115/116 对应的模型、环境、任务选择和动作计数；[评测指南](docs/evaluation.zh-CN.md)介绍初始化、评分和复现条件，[AndroidWorld 适配器](docs/androidworld-benchmark.md)说明基准接入方法。

## 文档导航

| 文档 | 内容 | English |
| --- | --- | --- |
| [PC 助手 MCP 接入](docs/local-mcp.zh-CN.md) | 接入、八个任务工具、进展与暂停恢复。 | [English](docs/local-mcp.md) |
| [桌面安装与连接](docs/desktop-setup.zh-CN.md) | 安装器、订阅/API、Wi-Fi/USB、注册与升级。 | [English quickstart](docs/local-mcp.md#desktop-setup) |
| [技术概览](docs/reliability-design.zh-CN.md) | 设计目标与核心机制：为什么这样组织 Agent。 | [English](docs/reliability-design.md) |
| [架构详解](docs/architecture.zh-CN.md) | 模块、接口与执行流程，以及各模块的详细设计。 | [English](docs/architecture.md) |
| [设计取舍](docs/design-decisions.zh-CN.md) | 审核、上下文、采集与输入的设计选择。 | [English](docs/design-decisions.md) |
| [部署指南](docs/deployment.zh-CN.md) | 模型、设备、安装与远程运行。 | [English](docs/deployment.md) |
| [可观测性](docs/observability.zh-CN.md) | Console 操作、轨迹检查与性能分析。 | [English](docs/observability.md) |
| [任务实例](docs/task-examples.zh-CN.md) | 自然语言任务示例和结果验证。 | [English](docs/task-examples.md) |
| [评测指南](docs/evaluation.zh-CN.md) | 基准配置、评分与复现。 | [English](docs/evaluation.md) |
| [Skills 指南](skills/README.zh-CN.md) | 应用知识、操作经验和技能编写。 | [English](skills/README.md) |
| [Skills 自进化](docs/skill-evolution.zh-CN.md) | 从真实任务提炼经验、独立审查、选择性验证与能力边界。 | [English](docs/skill-evolution.md) |

许可证：[Apache 2.0](LICENSE)。

第三方组件见[许可证说明](THIRD_PARTY_NOTICES.md)。


## 项目趋势

<a href="https://www.star-history.com/?repos=LordRosenberg%2FClickClick&amp;type=date&amp;legend=top-left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=LordRosenberg/ClickClick&amp;type=date&amp;theme=dark&amp;legend=top-left" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=LordRosenberg/ClickClick&amp;type=date&amp;legend=top-left" />
    <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=LordRosenberg/ClickClick&amp;type=date&amp;legend=top-left" />
  </picture>
</a>

<a href="https://github.com/LordRosenberg/ClickClick/releases">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/LordRosenberg/ClickClick/codex/androidworld-gallery/stats/installer-downloads-dark.svg" />
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/LordRosenberg/ClickClick/codex/androidworld-gallery/stats/installer-downloads.svg" />
    <img alt="ClickClick installer downloads" src="https://raw.githubusercontent.com/LordRosenberg/ClickClick/codex/androidworld-gallery/stats/installer-downloads.svg" />
  </picture>
</a>

Star 趋势由 Star History 自动更新，图表服务设有 24 小时缓存。安装器下载数每小时刷新，桌面正式版发布后也会刷新，按天展示累计下载数，图中标注最后更新时间。GitHub 图片缓存可能带来额外的显示延迟。仅统计 Windows/macOS 安装包，包含重复下载与升级下载，不代表独立用户人数；趋势从首次采样开始积累。
