"""Render bilingual, editable memory-design SVGs using only the standard library."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/assets"
INK, MUTED, LINE = "#203247", "#657487", "#DCE3EA"
BLUE, TEAL, AMBER = "#3D68A0", "#24786E", "#9C732F"


class Figure:
    def __init__(self, height, title, subtitle):
        self.height = height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="{height}" viewBox="0 0 1120 {height}" role="img" aria-labelledby="title desc">',
                      f'<title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle)}</desc>',
                      '<defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0L6 3.5L0 7" fill="none" stroke="#8292A4" stroke-width="1.3"/></marker></defs>',
                      f'<rect width="1120" height="{height}" rx="12" fill="#FFFFFF"/>']
        self.text(40, 43, title, 25, INK, 650)
        self.text(40, 73, subtitle, 15)
        self.line(40, 95, 1080, 95)

    def text(self, x, y, value, size=16, color=MUTED, weight=400):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI, Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(value)}</text>')

    def rect(self, x, y, w, h, fill="#F6F8FA", stroke="none", radius=8):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')

    def line(self, x, y, x2, y2, arrow=False, color=LINE):
        marker = ' marker-end="url(#arrow)"' if arrow else ""
        self.parts.append(f'<path d="M{x} {y} L{x2} {y2}" fill="none" stroke="{color}" stroke-width="1.5"{marker}/>')

    def save(self, name, lang):
        (OUT / f"memory-{name}{'.zh-CN' if lang == 'zh' else ''}.svg").write_text("\n".join(self.parts + ['</svg>']) + '\n', encoding='utf-8')


def overview(lang):
    zh = lang == 'zh'
    f = Figure(425, '保存完整，使用有界' if zh else 'Preserve the record. Bound the context.',
               '三条信息通路分别负责可追溯、记忆与过程衔接' if zh else 'Three information paths for traceability, working memory and continuity')
    headers = ['持久保存', '如何进入上下文', '当前决策'] if zh else ['PERSISTENT STORAGE', 'CONTEXT PROJECTION', 'CURRENT DECISION']
    for x, t in zip([40, 407, 834], headers):
        f.text(x, 125, t, 13, MUTED, 600)
    rows = [
        ('原始记录', '观测 · 动作 · 对话', '最近步骤 / 按来源回读', BLUE, '#EEF3FA'),
        ('工作笔记', '模型选择 · 版本留存', '目录 / 关键内容原样恢复', TEAL, '#EDF6F3'),
        ('结构化摘要', '较早历史的关键结果', '三栏摘要 / 来源按需读取', AMBER, '#F8F4EA'),
    ] if zh else [
        ('Original records', 'Observations · actions · dialogue', 'Recent steps / source retrieval', BLUE, '#EEF3FA'),
        ('Working notes', 'Model-selected · versioned', 'Index / exact retained text', TEAL, '#EDF6F3'),
        ('Structured summary', 'Consequential earlier outcomes', 'Three sections / source retrieval', AMBER, '#F8F4EA'),
    ]
    for y, (title, sub, route, color, fill) in zip([145, 222, 299], rows):
        f.rect(40, y, 310, 64, fill)
        f.rect(40, y + 12, 3, 40, color, radius=1)
        f.text(58, y + 26, title, 18, INK, 600)
        f.text(58, y + 49, sub, 14)
        f.line(362, y + 32, 393, y + 32, True)
        f.text(407, y + 38, route, 16)
        f.line(726, y + 32, 809, y + 32, True)
    f.rect(823, 145, 257, 218, '#F5F7FA', LINE)
    for y, t, size, color in [(179, '模型上下文' if zh else 'Model context', 21, INK),
                              (217, '最新状态 + 相关信息' if zh else 'Latest state + relevant detail', 15, MUTED),
                              (261, '不必重读全部历史' if zh else 'No full-history reread', 16, INK),
                              (300, '存在关键缺口时回源' if zh else 'Retrieve material gaps', 15, MUTED)]:
        f.text(844, y, t, size, color)
    f.text(40, 397, '设计边界：摘要不改写笔记；保留内容不等于已验证事实。' if zh else 'Boundary: summaries cannot edit notes; retained content is not verified truth.', 15)
    f.save('overview', lang)


def admission(lang):
    zh = lang == 'zh'
    f = Figure(395, '什么时候值得写笔记？' if zh else 'When is a note worth keeping?',
               '先判断后续价值，再决定新建或更新；不把动作日志复制一遍' if zh else 'Select for future use; do not duplicate the action log')
    cards = [('后续需要', '会影响之后的操作', '或最终交付'), ('容易丢失', '可能离开上下文', '且重新获取有成本'), ('确有增量', '尚未充分保留', '或有纠正与补充')] if zh else [
        ('Needed later', 'Affects later work', 'or the final answer'), ('Costly to recover', 'May leave available context', 'and require work to retrieve'), ('Adds information', 'Not adequately retained yet', 'or needs correction')]
    for i, (title, a, b) in enumerate(cards):
        x = 40 + i * 355
        f.rect(x, 122, 330, 145, '#F5F8FB')
        f.text(x + 20, 151, f'0{i+1}', 13, BLUE, 650)
        f.text(x + 20, 186, title, 22, INK, 600)
        f.text(x + 20, 216, a, 16)
        f.text(x + 20, 240, b, 16)
    f.rect(40, 288, 1040, 62, '#EDF6F3')
    f.text(60, 314, '满足三个条件 → 新主题建笔记，同一主题更新原 key' if zh else 'All three apply → create a new subject or update its existing key', 17, TEAL, 600)
    f.text(60, 338, '否则不额外记录；已有原始历史仍可回查。' if zh else 'Otherwise, skip the extra note. Original history remains available.', 14)
    f.text(40, 378, '这是一条模型记录指引，不是运行时的语义判定门槛。' if zh else 'This guides model selection; it is not a runtime semantic gate.', 14)
    f.save('admission', lang)


def compaction(lang):
    zh = lang == 'zh'
    f = Figure(505, '旧历史如何变成下一次决策的上下文' if zh else 'From earlier history to the next decision',
               '超过历史预算，且替换预计能释放空间时，启动压缩' if zh else 'Compact when history exceeds its budget and replacement is expected to release space')
    for x, label in [(40, '01  输入' if zh else '01  INPUT'),
                     (408, '02  概括' if zh else '02  SUMMARIZE'),
                     (810, '03  校验' if zh else '03  VALIDATE')]:
        f.text(x, 126, label, 13, MUTED, 650)
    f.rect(40, 144, 302, 190, '#EEF3FA')
    f.text(60, 179, '本次待压缩内容' if zh else 'History to summarize', 20, INK, 600)
    for y, label in [(217, '较早对话的文字记录' if zh else 'Older dialogue text'),
                     (250, '上一版摘要及原始来源' if zh else 'Previous items and sources'),
                     (290, '保留笔记作为只读参照' if zh else 'Retained notes as reference')]:
        f.text(60, y, label, 16)
    f.line(355, 240, 393, 240, True)
    f.rect(408, 144, 348, 190, '#F8F4EA')
    f.text(428, 179, '独立压缩会话' if zh else 'Compaction session', 20, INK, 600)
    for y, label in [(217, '结果  results' if zh else 'results'),
                     (250, '尝试  decisions_and_attempts' if zh else 'decisions_and_attempts'),
                     (283, '细节  critical_context' if zh else 'critical_context')]:
        f.text(428, y, label, 17, AMBER)
    f.line(769, 240, 795, 240, True)
    f.rect(810, 144, 270, 190, '#F5F7FA', LINE)
    f.text(830, 179, '运行时校验' if zh else 'Runtime checks', 20, INK, 600)
    for y, label in [(217, '格式 / 来源可解析' if zh else 'Shape / resolvable sources'),
                     (250, '笔记副本全文一致' if zh else 'Exact note copies'),
                     (283, '渲染后 ≤ 4,000 字符' if zh else 'Rendered text ≤ 4,000 chars')]:
        f.text(830, y, label, 15)
    f.text(40, 366, '无效提交 → 返回纠正，原历史保持不变' if zh else 'Invalid submission → correction; active history stays intact', 16, MUTED)
    f.line(945, 341, 945, 386, True, TEAL)
    f.text(965, 367, '有效' if zh else 'Valid', 14, TEAL, 600)
    f.rect(40, 395, 1040, 67, '#EDF6F3')
    f.text(60, 421, '下一次决策' if zh else 'NEXT DECISION', 13, TEAL, 650)
    f.text(60, 448, '结构化摘要  +  最近两步原文及回执  +  独立笔记  +  当前状态' if zh else
           'Structured summary  +  latest two complete steps  +  independent notes  +  current state', 18, INK, 600)
    f.text(40, 491, '原始记录持续可查；校验确认格式和引用，不代替事实判断。' if zh else
           'Original records remain readable. Validation checks format and references, not factual truth.', 14)
    f.save('compaction', lang)


def storage(lang):
    zh = lang == 'zh'
    f = Figure(460, '存储与上下文的分工' if zh else 'Storage and context have different jobs',
               '数据库和文件持久保存；内存负责当前运行；上下文是发送给模型的选择性视图' if zh else 'Database and files persist; memory runs the task; context is the selected model input')
    for x, title, path, detail in [
        (40, 'SQLite 数据库' if zh else 'SQLite database', '<data_dir>/clickclick.db',
         '任务状态 · 笔记版本 · 记录与文件引用' if zh else 'Task state · note versions · records and file references'),
        (600, '产物文件' if zh else 'Artifact files', '<data_dir>/artifacts/',
         '图片 · 完整对话 JSON · 模型请求快照' if zh else 'Images · full dialogue JSON · request snapshots'),
    ]:
        f.rect(x, 120, 480, 118, '#EEF3FA')
        f.text(x + 20, 152, title, 21, INK, 600)
        f.text(x + 20, 180, path, 16, BLUE)
        f.text(x + 20, 211, detail, 15)
    f.line(525, 178, 592, 178, True)
    f.text(535, 163, '引用' if zh else 'refs', 13)
    f.line(280, 243, 280, 298, True)
    f.line(840, 243, 540, 298, True)
    f.text(62, 276, '按需读取' if zh else 'Read as needed', 14)
    f.rect(40, 306, 670, 102, '#EDF6F3')
    f.text(60, 340, 'Python 运行内存' if zh else 'Python runtime memory', 21, INK, 600)
    f.text(60, 376, '工作副本 · 加载记录 · 组装消息 · 临时缓存' if zh else 'Working state · loaded records · assembled messages · temporary caches', 16)
    f.line(722, 355, 813, 355, True)
    f.text(733, 339, '选择' if zh else 'Select', 14, TEAL)
    f.rect(825, 306, 255, 102, '#F5F7FA', LINE)
    f.text(845, 340, '模型上下文' if zh else 'Model context', 21, INK, 600)
    f.text(845, 376, '本次请求的信息' if zh else 'Input for this request', 16)
    f.text(40, 440, '运行结果分别保存到数据库与文件；临时缓存不等于持久记忆。' if zh else 'Runtime saves results to the database and files; temporary caches are not durable memory.', 14)
    f.save('storage', lang)


if __name__ == '__main__':
    for language in ('zh', 'en'):
        overview(language)
        admission(language)
        compaction(language)
        storage(language)
