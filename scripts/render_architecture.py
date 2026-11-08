"""Regenerate the bilingual README architecture diagrams with no dependencies."""
from html import escape
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/clickclick-architecture.svg"
OUTPUT_ZH = ROOT / "docs/assets/clickclick-architecture.zh-CN.svg"
INK, MUTED, BLUE, GREEN, PURPLE = "#263449", "#586575", "#456B96", "#287568", "#80618E"

ENGLISH = {'用户': 'User',
 'PC AI 助手 / MCP Client': 'PC AI assistant / MCP Client',
 'Console / 浏览器': 'Console / browser',
 'HITL 与可视化': 'HITL and visualization',
 'Control API / 任务管理': 'Control API / task management',
 '任务 / 状态 / 控制': 'Tasks / status / control',
 '问题 / 回答': 'Questions / replies',
 '状态 / 证据': 'State / evidence',
 'Android 真机 / 模拟器': 'Android device / emulator',
 '任务控制 / 动作': 'Task control / actions',
 '可选 / 依宿主支持': 'Optional / host-dependent',
 '逻辑模块示意，非进程部署图': 'Logical modules, not deployment units',
 'Agent Harness、可靠观测、持久 Session 与 Skills 自学习': 'Agent harness, reliable observation, persistent '
                                               'session and skill learning',
 '任务执行、证据持久化与知识改进形成闭环': 'Task execution, durable evidence and reusable knowledge form a continuous '
                        'loop',
 '目标 · 修改 · 回复': 'Goals · changes · replies',
 '交办任务 · 查询 / 控制 · 回答问题': 'Delegate tasks · query / control · answer questions',
 '链接': 'Link',
 'MCP Server / 任务接口': 'MCP server / task interface',
 '本地后台 · 任务独立于客户端连接': 'Local backend · task lifetime is independent of client connections',
 '生命周期 · 设备分配 · 幂等 · 暂停 / 恢复 · 预算 · HITL · 进度': 'Lifecycle · device allocation · idempotency · '
                                                'pause / resume · budgets · HITL · progress',
 '任务控制': 'Task control',
 '模型网关': 'Model gateway',
 'Reviewer · 按需审核': 'Reviewer · as needed',
 'Session / 持久会话与存储': 'Session / persistent state',
 '可靠观测与设备执行': 'Reliable observation and device execution',
 '观测包与预处理': 'Observation package and preprocessing',
 '应用状态与真实执行环境': 'App state and execution environment',
 '动作派发与回执': 'Action dispatch and receipts',
 '外部评测': 'External evaluation',
 '官方初始化与评分 · 成功率 / P50-P95 / Token / 轨迹分析': 'Official setup / scoring · success rate / P50-P95 / '
                                            'tokens / trace analysis',
 'Skills 自学习': 'Skills self-improvement',
 '独立 Skill Reviewer': 'Independent Skill Reviewer',
 '按需探索与实测': 'Exploration and validation',
 'Skills 库 / 版本管理': 'Skills library / versions',
 '在线任务控制': 'Live task control',
 '上下文组装': 'Context assembly',
 '稳定前缀与动态内容分离': 'Separate stable / dynamic content',
 '分层记忆与压缩': 'Memory and compaction',
 'Skills 渐进式披露': 'Progressive skill disclosure',
 '工具会话与执行控制': 'Tool session and execution control',
 '持久化与异常恢复': 'Durability and recovery',
 '进程内存 / 运行副本': 'Process memory / runtime state',
 'scrcpy / Collector · 时序与窗口对齐': 'scrcpy / Collector · time / window alignment',
 '归一化 / SoM / 几何 · 新鲜度': 'Normalization / SoM / geometry · freshness',
 'snapshot / sequence / 原生 detail': 'snapshot / sequence / native detail',
 'ADB / IME · 目标 / 焦点 / 输入读回': 'ADB / IME · target / focus / input readback',
 'operation ID 去重 · journal / 回执': 'Operation ID dedup · journal / receipts',
 '本机或远程 Driver': 'Local or remote Driver',
 '失败 / 绕路 → 条件化修订': 'Failures / detours → revisions',
 '独立上下文 · 六维审核': 'Independent context · six review criteria',
 '隔离候选库 · 结果 / 成本': 'Isolated candidates · outcomes / cost',
 '确认结果后入库 · 版本化交付': 'Confirm results · publish versioned skills',
 '审查 / 实测反馈修订；人工主要复核结果与候选差异': 'Revise from review / tests; human review focuses on results and '
                              'candidate changes',
 '状态查询 · 任务委托 · 进度与历史': 'Readiness · task delegation · progress and history',
 '任务控制 · HITL 介入 · 可视化链接': 'Task control · HITL steering · viewing links',
 '角色编排与重规划': 'Role orchestration and replanning',
 '持久记录与运行副本分离': 'Durable records and runtime state kept separate',
 '数据库 / SQLite': 'Database / SQLite',
 '文件 / 产物存储': 'Files / artifact storage',
 '按角色选择模型与协议': 'Select model / protocol by role',
 '记录调用、Token 与费用': 'Record calls, tokens and cost',
 '持久保存用户修改与问答': 'Persist user changes and replies',
 '指令版本使旧模型响应失效': 'Invalidate stale model responses',
 '阶段计划可修订，按执行反馈切换角色': 'Revise stage plans and roles from execution feedback',
 '按角色组装任务、阶段和观测': 'Task, stage and view for each role',
 '按预算保留历史与工作记忆': 'Bound history and working memory',
 '原始轨迹、笔记、摘要分层保存': 'Raw traces, notes and summaries',
 '压缩旧历史，近期原文可回查': 'Compact history; recall originals',
 '可选 Jev 核验摘要忠实度': 'Optional Jev summary fidelity checks',
 '按应用、设备和阶段筛选技能': 'Select by app, device and stage',
 '先目录后正文，按角色交付': 'Catalog first; deliver role sections',
 '记录实际加载的版本与哈希': 'Record loaded versions and hashes',
 '按角色开放类型化工具与有界会话': 'Role-scoped typed tools and bounded sessions',
 '绑定当前观测与目标，校验动作输入': 'Bind current observation / target; validate actions',
 '统一处理预算、取消、回执与错误': 'Handle budgets, cancellation, receipts and errors',
 '意图、回执、步骤状态分阶段持久化': 'Persist intent, receipt and step state in stages',
 '检查点续跑，代次隔离旧执行': 'Resume checkpoints; fence previous execution',
 '结果未知不重放，取新观测重新决策': 'Unknown: no replay; observe afresh and decide',
 '任务状态': 'Task state',
 '工作记忆': 'Working memory',
 '执行记录': 'Execution records',
 '交互原文': 'Raw interactions',
 '观测产物': 'Observations',
 '恢复正文': 'Recovery',
 '运行副本': 'Runtime state',
 '加载缓存': 'Loaded content',
 '计划 / 预算 / 检查点': 'plan / budgets / checkpoints',
 '笔记版本 / 摘要 / 来源': 'note versions / summary / sources',
 '意图 / 回执 / 步骤 / Trace': 'intents / receipts / steps / traces',
 '对话 / 模型请求与响应': 'dialogues / model requests / replies',
 '截图 / UI 树 / 标注图': 'screenshots / UI trees / annotations',
 '清单 / 提示词 / Skill 快照': 'manifests / prompts / skill snapshots',
 '角色会话 / 当前观测': 'role sessions / current observation',
 'Skills / 提示词 / 临时数据': 'skills / prompts / transient caches',
 '任务、投屏、模型输出与学习结果': 'Tasks, screen, model output and learning results',
 '宿主桥接 / Console 问答 · 只读投屏': 'Reply bridge / Console answers · read-only screen',
 'Session 轨迹 → 独立审查与实测 → Skills 库 → Harness 按需交付': 'Session traces → review and execution checks → '
                                                   'skill library → Harness delivery',
 '观测 / 动作 / 回执': 'Observation / actions / receipts',
 '读写': 'R/W'}


def render_figure(language="en"):
    parts = [
      '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1450" viewBox="0 0 1600 1450" role="img" aria-labelledby="title desc">',
      '<title id="title">ClickClick core architecture: harness, observation, session and skill learning</title>',
      '<desc id="desc">Lightweight MCP and Console interfaces connect to task management. The core is the Agent Harness, reliable device observation, durable Session and skill self-improvement. Skill Reviewer and selected execution checks establish quality; users inspect review and validation results and confirm publication. Published versions feed runtime skill delivery.</desc>',
      '<defs>',
    ]
    for name, color in [('control', INK), ('evidence', GREEN), ('optional', PURPLE), ('support', INK)]:
        parts.append(f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 1L9 5L0 9Z" fill="{color}"/></marker>')
    parts += ['</defs>', '<rect width="1600" height="1450" fill="white"/>']
    def tr(value):
        if language == 'en' and re.search('[\u4e00-\u9fff]', value):
            return ENGLISH[value]
        return value
    def text(x, y, value, size=17, color=INK, weight=400, gap=23, anchor='start'):
        lines = [value] if isinstance(value, str) else value
        body = ''.join(f'<tspan x="{x}" dy="{0 if i==0 else gap}">{escape(tr(line))}</tspan>' for i,line in enumerate(lines))
        parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI, Microsoft YaHei, Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{body}</text>')
    def rect(x, y, w, h, fill='white', color='#CCD3DC', width=1.3, dash=False):
        extra=' stroke-dasharray="6 5"' if dash else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{color}" stroke-width="{width}"{extra}/>')
    def arrow(d, kind='control', both=False, dash=False):
        color={'control':INK,'evidence':GREEN,'optional':PURPLE,'support':INK}[kind]
        extra=f' marker-start="url(#{kind})"' if both else ''
        if dash: extra += ' stroke-dasharray="6 5"'
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round" marker-end="url(#{kind})"{extra}/>')
    def support(x,y,w,h,title,lines,color=BLUE,fill='#EDF3FA'):
        rect(x,y,w,h,fill=fill,color=color)
        text(x+16,y+26,title,20,color,500)
        text(x+16,y+52,lines,16,MUTED,gap=21)
    def core_node(x,y,w,h,title,lines,color=BLUE,fill='white'):
        rect(x,y,w,h,fill,color)
        text(x+16,y+29,title,21,INK if color==BLUE else color,500)
        text(x+16,y+56,lines,16,MUTED,gap=22)
    def storage_node(y,h,title,groups):
        rect(1105,y,430,h,'white',GREEN)
        text(1121,y+29,title,21,GREEN,500)
        rows=[]
        for i,(label,value) in enumerate(groups):
            label=escape(tr(label)); value=escape(tr(value))
            colon=':\u00a0' if language=='en' else '：'
            rows.append(f'<tspan x="1121" dy="{0 if i==0 else 25}"><tspan fill="{INK}">{label}{colon}</tspan><tspan fill="{MUTED}">{value}</tspan></tspan>')
        parts.append(f'<text x="1121" y="{y+56}" font-family="Segoe UI, Microsoft YaHei, Arial, sans-serif" font-size="16" font-weight="400">'+''.join(rows)+'</text>')

    text(40,44,'CLICKCLICK',29,INK,700)
    text(255,44,'Agent Harness、可靠观测、持久 Session 与 Skills 自学习',25,MUTED)
    text(40,78,'任务执行、证据持久化与知识改进形成闭环',18,MUTED)

    support(40,100,220,70,'用户',['目标 · 修改 · 回复'],INK,'#F5F8FC')
    support(310,100,650,70,'PC AI 助手 / MCP Client',['交办任务 · 查询 / 控制 · 回答问题'])
    support(1100,100,460,70,'Console / 浏览器',['任务、投屏、模型输出与学习结果'],INK,'#EEF7F3')
    arrow('M260 135H310','support',both=True)
    arrow('M960 135H1100','support',dash=True)
    text(1030,122,'链接',14,MUTED,anchor='middle')
    arrow('M650 170V195','support',both=True)
    text(670,189,'stdio / Streamable HTTP',14,MUTED)

    rect(40,195,980,55,'#EDF3FA',BLUE)
    text(56,229,'MCP Server / 任务接口',18,BLUE)
    text(320,218,'状态查询 · 任务委托 · 进度与历史',16,INK)
    text(320,240,'任务控制 · HITL 介入 · 可视化链接',16,INK)
    rect(1100,195,460,55,'#EDF3FA',BLUE)
    text(1116,218,'HITL 与可视化',20,INK,500)
    text(1116,240,'宿主桥接 / Console 问答 · 只读投屏',16,INK)
    arrow('M1350 195V170','support')
    arrow('M1020 223H1100','optional',both=True,dash=True)
    text(1060,211,'HITL',14,PURPLE,anchor='middle')
    arrow('M650 250V285','support',both=True)
    text(670,277,'任务 / 状态 / 控制',14,MUTED)
    text(40,277,'本地后台 · 任务独立于客户端连接',15,MUTED)
    rect(40,285,1520,55,'#F1F6FC',BLUE)
    text(56,319,'Control API / 任务管理',18,BLUE)
    text(430,319,'生命周期 · 设备分配 · 幂等 · 暂停 / 恢复 · 预算 · HITL · 进度',17,MUTED)
    arrow('M1350 250V285','optional',both=True,dash=True)
    text(1367,277,'问题 / 回答',14,PURPLE)

    # Most of the central area belongs to Harness internals, rather than learning.
    parts.append('<g transform="translate(0,-55)">')
    rect(40,430,980,560,'#F7FAFE',BLUE)
    text(65,471,'Agent Harness',25,BLUE,500)
    arrow('M980 395V414H335V495',both=True)
    text(365,411,'任务控制',15,MUTED)
    core_node(635,495,360,90,'模型网关',[
      '按角色选择模型与协议','记录调用、Token 与费用'])
    core_node(635,605,360,85,'在线任务控制',[
      '持久保存用户修改与问答','指令版本使旧模型响应失效'])
    arrow('M635 540H605',both=True)
    arrow('M635 647H605','optional',dash=True)

    rect(65,495,540,195,'#EDF3FA',BLUE)
    text(85,527,'角色编排与重规划',23,BLUE,500)
    rect(85,548,185,48,'white',BLUE)
    rect(400,548,185,48,'white',BLUE)
    text(177.5,580,'Planner',21,INK,500,anchor='middle')
    text(492.5,580,'Executor',21,INK,500,anchor='middle')
    arrow('M270 563H400')
    arrow('M400 583H270')
    rect(205,614,260,40,'#F4EEF7',PURPLE,dash=True)
    text(335,641,'Reviewer · 按需审核',19,PURPLE,500,anchor='middle')
    arrow('M177.5 596V632H205','optional',dash=True)
    arrow('M492.5 596V632H465','optional',dash=True)
    text(85,678,'阶段计划可修订，按执行反馈切换角色',17,INK)

    # A single supporting panel avoids a web of edges between input capabilities.
    rect(635,710,360,270,'#EEF7F3',GREEN)
    for i,(heading,lines) in enumerate([
      ('上下文组装',[
        '按角色组装任务、阶段和观测','稳定前缀与动态内容分离',
        '按预算保留历史与工作记忆']),
      ('分层记忆与压缩',[
        '原始轨迹、笔记、摘要分层保存','压缩旧历史，近期原文可回查',
        '可选 Jev 核验摘要忠实度']),
      ('Skills 渐进式披露',[
        '按应用、设备和阶段筛选技能','先目录后正文，按角色交付',
        '记录实际加载的版本与哈希']),
    ]):
        section_y=710+i*90
        text(651,section_y+24,heading,20,GREEN,500)
        text(651,section_y+46,lines,16,MUTED,gap=19)
        if i<2: parts.append(f'<path d="M651 {section_y+90}H979" stroke="#D7E2DD" stroke-width="1"/>')
    arrow('M635 755H620V675H605','evidence')

    core_node(65,720,540,110,'工具会话与执行控制',[
      '按角色开放类型化工具与有界会话',
      '绑定当前观测与目标，校验动作输入',
      '统一处理预算、取消、回执与错误'])
    core_node(65,860,540,110,'持久化与异常恢复',[
      '意图、回执、步骤状态分阶段持久化',
      '检查点续跑，代次隔离旧执行',
      '结果未知不重放，取新观测重新决策'])
    arrow('M335 690V720',both=True)
    arrow('M335 830V860',both=True)

    rect(1080,430,480,560,'#EEF7F3',GREEN)
    text(1105,471,'Session / 持久会话与存储',25,GREEN,500)
    text(1105,508,'持久记录与运行副本分离',17,MUTED)
    storage_node(535,135,'数据库 / SQLite',[
      ('任务状态','计划 / 预算 / 检查点'),
      ('工作记忆','笔记版本 / 摘要 / 来源'),
      ('执行记录','意图 / 回执 / 步骤 / Trace')])
    storage_node(690,135,'文件 / 产物存储',[
      ('交互原文','对话 / 模型请求与响应'),
      ('观测产物','截图 / UI 树 / 标注图'),
      ('恢复正文','清单 / 提示词 / Skill 快照')])
    storage_node(845,120,'进程内存 / 运行副本',[
      ('运行副本','角色会话 / 当前观测'),
      ('加载缓存','Skills / 提示词 / 临时数据')])
    arrow('M1320 395V430','evidence',both=True)
    text(1340,416,'状态 / 证据',15,GREEN)
    arrow('M1020 785H1080','evidence',both=True)
    text(1050,770,'读写',14,GREEN,anchor='middle')
    parts.append('</g>')

    rect(40,975,1520,160,'#F5F9F7',GREEN)
    text(65,1004,'可靠观测与设备执行',25,GREEN,500)
    core_node(65,1017,460,105,'观测包与预处理',[
      'scrcpy / Collector · 时序与窗口对齐',
      '归一化 / SoM / 几何 · 新鲜度',
      'snapshot / sequence / 原生 detail'],GREEN)
    core_node(590,1027,420,85,'Android 真机 / 模拟器',[
      '应用状态与真实执行环境'],INK,'#EDF3FA')
    core_node(1075,1017,460,105,'动作派发与回执',[
      'ADB / IME · 目标 / 焦点 / 输入读回',
      'operation ID 去重 · journal / 回执',
      '本机或远程 Driver'],GREEN)
    arrow('M590 1070H525','evidence')
    arrow('M1075 1070H1010')
    arrow('M335 935V975',both=True)
    text(355,961,'观测 / 动作 / 回执',15,MUTED)

    rect(40,1160,1520,45,'#EDF3FA',BLUE)
    text(65,1189,'外部评测',18,BLUE)
    text(310,1189,'官方初始化与评分 · 成功率 / P50-P95 / Token / 轨迹分析',17,MUTED)
    arrow('M800 1135V1160','evidence',both=True)

    # A short learning lane retains the full improvement loop and result review.
    rect(40,1240,1520,145,'#F7FAFE',BLUE)
    text(65,1270,'Skills 自学习',23,BLUE,500)
    text(395,1270,'Session 轨迹 → 独立审查与实测 → Skills 库 → Harness 按需交付',16,MUTED)
    core_node(65,1284,280,69,'Learner',['失败 / 绕路 → 条件化修订'],INK)
    core_node(395,1284,320,69,'独立 Skill Reviewer',['独立上下文 · 六维审核'],INK)
    core_node(765,1284,310,69,'按需探索与实测',['隔离候选库 · 结果 / 成本'],INK)
    core_node(1125,1284,410,69,'Skills 库 / 版本管理',[
      '确认结果后入库 · 版本化交付'],GREEN,'#EEF7F3')
    arrow('M345 1318H395')
    arrow('M715 1318H765','optional',dash=True)
    arrow('M1075 1318H1125','evidence')
    arrow('M555 1353V1365H205V1353','optional',dash=True)
    text(600,1375,'审查 / 实测反馈修订；人工主要复核结果与候选差异',15,MUTED)

    arrow('M44 1422H95')
    text(108,1428,'任务控制 / 动作',16)
    arrow('M365 1422H416','evidence')
    text(430,1428,'状态 / 证据',16,GREEN)
    arrow('M650 1422H701','optional',dash=True)
    text(715,1428,'可选 / 依宿主支持',16,PURPLE)
    text(1560,1428,'逻辑模块示意，非进程部署图',16,MUTED,anchor='end')
    return '\n'.join(parts+['</svg>'])+'\n'


def main():
    for language, output in (("en", OUTPUT), ("zh-CN", OUTPUT_ZH)):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_figure(language), encoding="utf-8")
        print(output)


if __name__ == "__main__":
    main()
