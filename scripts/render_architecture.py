"""Regenerate the bilingual README architecture diagrams with no dependencies."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/clickclick-architecture.svg"
OUTPUT_ZH = ROOT / "docs/assets/clickclick-architecture.zh-CN.svg"
INK, MUTED, BLUE, GREEN, PURPLE = "#263449", "#586575", "#456B96", "#287568", "#80618E"

ENGLISH = {
    '助手接入、任务控制与 Android 执行': 'Assistant integration, task control and Android execution',
    'MCP 委托完整任务，ClickClick 使用自己的模型与 Harness 执行': 'Delegate complete tasks through MCP · ClickClick runs its own models and harness',
    '用户': 'User',
    '交办目标 · 补充要求 · 回答问题': 'Goals · changes · replies',
    'PC AI 助手 / MCP Client': 'PC AI assistant / MCP Client',
    '理解用户请求 · 调用任务工具 · 汇报进展和结果': 'Understand requests · invoke task tools · report progress and results',
    'Console / 浏览器': 'Console / browser',
    '任务详情 · 手机画面 · 模型输出': 'Task details · screen · model output',
    '打开链接': 'Link',
    'MCP Server · 9 个任务工具': 'MCP Server · 9 task tools',
    '任务级委托与控制 · 返回任务 ID、状态、结果及查看链接': 'Task-level delegation · IDs, status, results and viewing links',
    '就绪检查与派发': 'Readiness and delegation',
    '进度、历史与截图': 'Progress and history',
    'get_task_snapshot（已保存截图）': 'get_task_snapshot (saved)',
    '调整与执行控制': 'Changes and control',
    'steer_task（HITL 开启时）': 'steer_task (HITL enabled)',
    'HITL 与可视化': 'HITL and visualization',
    '宿主支持回复桥接时，可在助手中问答': 'Reply bridge: answer inside the assistant',
    '其他宿主通过 Console 回答': 'Otherwise use Console to answer',
    '浏览器打开只读投屏 + 模型输出': 'Read-only screen + model output',
    'MCP 返回链接，画面由网页展示': 'MCP returns links; the browser shows frames',
    '暂停 / 结束后标记最后收到的画面': 'Paused / ended: label the last received frame',
    'CLICKCLICK 本地后台 · 助手连接断开后，后台任务仍可继续执行': 'LOCAL BACKEND · Tasks outlive client connections',
    'Control API / 任务管理': 'Control API / task management',
    '任务生命周期 · 设备分配 · 幂等请求 · 暂停与恢复 · 明确追加预算 · HITL 问题 / 回答 · 进度与页面链接': 'Lifecycle · device allocation · idempotency · pause / resume · explicit budget increments · HITL questions / replies · progress and links',
    '任务 / 状态 / 控制': 'Tasks / status / control',
    '问题 / 回答': 'Questions / replies',
    'ClickClick 配置的模型 / Model': 'Models configured in ClickClick',
    '独立 API 或订阅授权 · 按角色路由 · 理解任务与证据 · 推理和决策': 'Own API or subscription access · role routing · task and evidence interpretation · reasoning and decisions',
    '派发 / 调整 / 执行控制': 'Delegate / revise / control',
    '模型输入 / 输出': 'Model I/O',
    '角色路由 · 协议适配 · 调用与用量记录': 'Role routing · protocols · call / usage records',
    '上下文、记忆访问与 Skills': 'Context, memory and skills',
    '原始任务 · 当前阶段 · 新鲜观测': 'Original task · stage · fresh observation',
    '结构化摘要 · 保留笔记 · 近期步骤': 'Structured summary · notes · recent steps',
    '来源回读 · 按作用域选择和交付技能': 'Source recall · scoped skill selection / delivery',
    '上下文': 'Context',
    'Agent loop / 角色编排': 'Agent loop / role orchestration',
    'Reviewer · 按需介入': 'Reviewer · on demand',
    '反馈 → 继续 / 重规划 / 复核 / 完成': 'Feedback → continue / replan / review / finish',
    '工具与执行控制': 'Tools and execution control',
    '观测与目标绑定 · 动作派发 · 输入核验 · 预算与取消 · 错误反馈': 'Observation / target binding · actions · input checks · budgets / cancellation · error feedback',
    'Session / 持久会话': 'Session / persistent records',
    '独立于模型上下文窗口保存': 'Retained independently of the',
    '任务状态、执行记录与证据': 'model context: state, history and evidence',
    '历史与事件': 'History and events',
    '模型 / 工具调用 · 动作回执': 'Model / tool calls · action receipts',
    '任务状态与笔记': 'Task state and notes',
    '计划与执行状态 · 版本化笔记': 'Plan / state · versioned notes',
    '观测与产物': 'Observations and artifacts',
    '截图 · UI 结构 · 身份与几何': 'Pixels · UI tree · identity / geometry',
    'SQLite 记录 + 产物文件': 'SQLite records + artifact files',
    '运行时 / Console / 审计读取': 'Read by runtime / Console / audits',
    '状态 / 证据': 'State / evidence',
    '回读': 'Read',
    '记录': 'Write',
    '采集与感知': 'Capture and perception',
    '截图 / UI 树 · 原生细节读取': 'Pixels / UI tree · native-detail reading',
    'Android 真机 / 模拟器': 'Android device / emulator',
    '应用与系统 · 持续变化的界面': 'Apps and OS · changing UI state',
    '执行环境': 'Execution environment',
    'ADB 动作 · IME 输入': 'ADB actions · IME input',
    '本机或远程设备宿主': 'Local or remote device host',
    '新鲜观测': 'Fresh observation',
    '动作 / 回执': 'Actions / receipts',
    '外部评测 / Evaluation': 'External evaluation',
    '受控环境初始化与官方评分 · Session 轨迹审计 · 质量 / 延迟 / 用量分析': 'Controlled setup + official scoring · trace audit · quality / latency / usage',
    '任务控制 / 动作': 'Task control / actions',
    '可选 / 依宿主支持': 'Optional / host-dependent',
    '逻辑模块示意，非进程部署图': 'Logical modules, not deployment units',
}

def render_figure(language="en"):
    def translate(value):
        if language != "en":
            return value
        if any("\u4e00" <= char <= "\u9fff" for char in value):
            return ENGLISH[value]
        return value

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1640" viewBox="0 0 1600 1640" role="img" aria-labelledby="title desc">',
        '<title id="title">ClickClick architecture: assistant, MCP and Android execution</title>',
        '<desc id="desc">PC assistants delegate complete Android tasks through nine MCP tools over stdio or Streamable HTTP. Control API manages tasks and the ClickClick harness runs its own configured models. Session retains state and evidence; Console shows traces and read-only previews. Compatible hosts can bridge HITL replies. Drivers act on Android, and external evaluators score outcomes.</desc>',
        '<defs>',
    ]
    for name, color in [("control", INK), ("evidence", GREEN), ("optional", PURPLE)]:
        parts.append(f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 1L9 5L0 9Z" fill="{color}"/></marker>')
    parts.extend(['</defs>', '<rect width="1600" height="1640" fill="white"/>'])


    def text(x, y, value, size=19, color=INK, weight=400, anchor="start", gap=27):
        lines = [value] if isinstance(value, str) else value
        lines = [translate(line) for line in lines]
        spans = ''.join(f'<tspan x="{x}" dy="{0 if i == 0 else gap}">{escape(line)}</tspan>' for i, line in enumerate(lines))
        parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI, Microsoft YaHei, Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{spans}</text>')


    def rect(x, y, w, h, fill="white", color="#CCD3DC", dash=False):
        dashed = ' stroke-dasharray="7 5"' if dash else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{color}" stroke-width="1.6"{dashed}/>')


    def box(x, y, w, h, title, lines=(), color=INK, fill="white", dash=False):
        rect(x, y, w, h, fill, color, dash)
        text(x+20, y+32, title, 23, color, 600)
        text(x+20, y+61, lines, 19, MUTED)


    def arrow(d, kind="control", both=False, dash=False):
        color = {"control": INK, "evidence": GREEN, "optional": PURPLE}[kind]
        start = f' marker-start="url(#{kind})"' if both else ''
        dashed = ' stroke-dasharray="7 5"' if dash else ''
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" marker-end="url(#{kind})"{start}{dashed}/>')


    text(40, 44, "CLICKCLICK", 29, INK, 700)
    text(255, 44, "助手接入、任务控制与 Android 执行", 26, MUTED)
    text(40, 78, "MCP 委托完整任务，ClickClick 使用自己的模型与 Harness 执行", 19)

    box(40, 110, 280, 95, "用户", ["交办目标 · 补充要求 · 回答问题"])
    box(380, 110, 700, 95, "PC AI 助手 / MCP Client", ["理解用户请求 · 调用任务工具 · 汇报进展和结果"], BLUE, "#EDF3FA")
    box(1140, 110, 420, 95, "Console / 浏览器", ["任务详情 · 手机画面 · 模型输出"])
    arrow("M320 157H380", both=True)
    arrow("M1080 157H1140", "evidence", dash=True)
    text(1110, 141, "打开链接", 15, GREEN, anchor="middle")
    arrow("M730 205V265", both=True)
    text(748, 241, "stdio / Streamable HTTP", 18)

    rect(40, 265, 1040, 225, "#EDF3FA", BLUE)
    text(60, 300, "MCP Server · 9 个任务工具", 25, BLUE, 600)
    text(60, 330, "任务级委托与控制 · 返回任务 ID、状态、结果及查看链接", 19, MUTED)
    rect(60, 351, 300, 119, "white")
    text(78, 379, "就绪检查与派发", 21, INK, 600)
    text(78, 408, ["get_status", "start_task"], 19)
    rect(390, 351, 300, 119, "white")
    text(408, 379, "进度、历史与截图", 21, INK, 600)
    text(408, 408, ["get_task · list_tasks", "get_task_snapshot（已保存截图）"], 18)
    rect(720, 351, 340, 119, "white")
    text(738, 379, "调整与执行控制", 21, INK, 600)
    text(738, 408, ["steer_task（HITL 开启时）", "pause_task · resume_task · cancel_task"], 17)

    rect(1140, 265, 420, 225, "#F7F8FA")
    text(1160, 300, "HITL 与可视化", 24, INK, 600)
    text(1160, 333, ["宿主支持回复桥接时，可在助手中问答", "其他宿主通过 Console 回答"], 18, MUTED)
    text(1160, 405, "浏览器打开只读投屏 + 模型输出", 20, GREEN, 600)
    text(1160, 436, ["MCP 返回链接，画面由网页展示", "暂停 / 结束后标记最后收到的画面"], 18, MUTED)
    arrow("M1350 265V205", "evidence")
    arrow("M1140 340H1080", "optional", both=True, dash=True)
    text(1110, 323, "HITL", 15, PURPLE, anchor="middle")

    text(40, 524, "CLICKCLICK 本地后台 · 助手连接断开后，后台任务仍可继续执行", 18, MUTED)
    box(40, 545, 1520, 85, "Control API / 任务管理", ["任务生命周期 · 设备分配 · 幂等请求 · 暂停与恢复 · 明确追加预算 · HITL 问题 / 回答 · 进度与页面链接"], BLUE, "#F7FAFE")
    arrow("M560 490V545", both=True)
    text(579, 520, "任务 / 状态 / 控制", 17)
    arrow("M1350 490V545", "optional", both=True, dash=True)
    text(1367, 520, "问题 / 回答", 17, PURPLE)
    arrow("M1560 160H1580V585H1560", both=True)

    box(40, 690, 1040, 85, "ClickClick 配置的模型 / Model", ["独立 API 或订阅授权 · 按角色路由 · 理解任务与证据 · 推理和决策"], PURPLE, "#F4EEF7")
    rect(40, 835, 1040, 445, "#F7FAFE", BLUE)
    text(65, 870, "Agent Harness", 27, BLUE, 600)
    arrow("M1100 630V810H820V890", both=True)
    text(838, 802, "派发 / 调整 / 执行控制", 17)
    arrow("M285 775V890", both=True)
    text(300, 813, "模型输入 / 输出", 17)

    box(70, 890, 430, 75, "Model gateway", ["角色路由 · 协议适配 · 调用与用量记录"])
    arrow("M500 927H570", both=True)
    box(70, 1010, 430, 150, "上下文、记忆访问与 Skills", ["原始任务 · 当前阶段 · 新鲜观测", "结构化摘要 · 保留笔记 · 近期步骤", "来源回读 · 按作用域选择和交付技能"], GREEN, "#EEF7F3")
    arrow("M500 1050H570", "evidence")
    text(535, 1032, "上下文", 15, GREEN, anchor="middle")

    rect(570, 890, 480, 270, "#EDF3FA", BLUE)
    text(590, 925, "Agent loop / 角色编排", 23, BLUE, 600)
    rect(590, 950, 180, 55, color=BLUE)
    rect(855, 950, 175, 55, color=BLUE)
    text(680, 985, "Planner", 23, weight=600, anchor="middle")
    text(942, 985, "Executor", 23, weight=600, anchor="middle")
    arrow("M770 968H855")
    arrow("M855 989H770")
    rect(675, 1040, 270, 50, "#F4EEF7", PURPLE, True)
    text(810, 1072, "Reviewer · 按需介入", 21, PURPLE, 600, "middle")
    arrow("M680 1005V1023H735V1040", "optional", dash=True)
    arrow("M942 1005V1023H885V1040", "optional", dash=True)
    text(590, 1135, "反馈 → 继续 / 重规划 / 复核 / 完成", 20)

    box(70, 1200, 980, 65, "工具与执行控制", ["观测与目标绑定 · 动作派发 · 输入核验 · 预算与取消 · 错误反馈"])
    arrow("M810 1160V1200", both=True)

    rect(1140, 690, 420, 590, "#EEF7F3", GREEN)
    text(1165, 727, "Session / 持久会话", 27, GREEN, 600)
    text(1165, 761, ["独立于模型上下文窗口保存", "任务状态、执行记录与证据"], 19, MUTED)
    box(1170, 830, 360, 95, "历史与事件", ["模型 / 工具调用 · 动作回执"], GREEN)
    box(1170, 950, 360, 95, "任务状态与笔记", ["计划与执行状态 · 版本化笔记"], GREEN)
    box(1170, 1070, 360, 95, "观测与产物", ["截图 · UI 结构 · 身份与几何"], GREEN)
    text(1170, 1215, "SQLite 记录 + 产物文件", 22, GREEN, 600)
    text(1170, 1247, "运行时 / Console / 审计读取", 18, MUTED)
    arrow("M1350 690V630", "evidence", both=True)
    text(1366, 670, "状态 / 证据", 17, GREEN)
    arrow("M1140 1050H1080", "evidence")
    text(1110, 1032, "回读", 15, GREEN, anchor="middle")
    arrow("M1080 1220H1140", "evidence")
    text(1110, 1248, "记录", 15, GREEN, anchor="middle")

    rect(40, 1340, 1520, 140, "#F5F9F7", GREEN)
    box(65, 1360, 450, 100, "采集与感知", ["scrcpy · Accessibility Collector", "截图 / UI 树 · 原生细节读取"], GREEN)
    box(570, 1360, 490, 100, "Android 真机 / 模拟器", ["应用与系统 · 持续变化的界面", "执行环境"], INK, "#EFF1F5")
    box(1115, 1360, 420, 100, "Device driver", ["ADB 动作 · IME 输入", "本机或远程设备宿主"])
    arrow("M570 1410H515", "evidence")
    arrow("M1115 1410H1060")
    arrow("M460 1360V1280", "evidence")
    text(478, 1320, "新鲜观测", 17, GREEN)
    arrow("M1000 1280V1310H1300V1360")
    text(1040, 1302, "动作 / 回执", 17)

    box(40, 1520, 1520, 60, "外部评测 / Evaluation", [])
    text(380, 1557, "受控环境初始化与官方评分 · Session 轨迹审计 · 质量 / 延迟 / 用量分析", 20)
    arrow("M815 1520V1480")
    arrow("M1560 1140H1580V1550H1560", "evidence")

    arrow("M44 1610H95")
    text(108, 1616, "任务控制 / 动作", 17)
    arrow("M365 1610H416", "evidence")
    text(430, 1616, "状态 / 证据", 17, GREEN)
    arrow("M650 1610H701", "optional", dash=True)
    text(715, 1616, "可选 / 依宿主支持", 17, PURPLE)
    text(1560, 1616, "逻辑模块示意，非进程部署图", 17, MUTED, anchor="end")

    return "\n".join(parts + ["</svg>"]) + "\n"


def main():
    for language, output in (("en", OUTPUT), ("zh-CN", OUTPUT_ZH)):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_figure(language), encoding="utf-8")
        print(output)


if __name__ == "__main__":
    main()
