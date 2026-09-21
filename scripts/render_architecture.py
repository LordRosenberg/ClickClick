"""Regenerate the README's editable architecture figure with no dependencies."""

from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/clickclick-architecture.svg"
INK = "#263449"
MUTED = "#586575"
BLUE = "#456B96"
GREEN = "#287568"
PURPLE = "#80618E"
RULE = "#CCD3DC"


class Figure:
    def __init__(self):
        self.parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1270" '
            'viewBox="0 0 1600 1270" role="img" aria-labelledby="title description">',
            '<title id="title">ClickClick: revisable planning with grounded device feedback</title>',
            '<desc id="description">Separate model and agent harness. A prominent Session '
            'column stands beside the harness and retains history, note versions and observation '
            'artifacts. Context management stays in the harness. Device tools and the Android '
            'environment execute actions; Console and evaluation consume Session records.</desc>',
            '<defs>',
        ]
        for name, color in [("control", INK), ("evidence", GREEN), ("review", PURPLE)]:
            self.parts.append(
                f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M 0 1 L 9 5 L 0 9 Z" fill="{color}"/></marker>'
            )
        self.parts.extend(['</defs>', '<rect width="1600" height="1270" fill="white"/>'])

    def rect(self, x, y, width, height, fill="white", stroke=RULE, dash=False, radius=5):
        dash_attr = ' stroke-dasharray="7 5"' if dash else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"{dash_attr}/>'
        )

    def text(self, x, y, lines, size=18, color=INK, weight=400, anchor="start", gap=25):
        if isinstance(lines, str):
            lines = [lines]
        spans = "".join(
            f'<tspan x="{x}" dy="{0 if i == 0 else gap}">{escape(line)}</tspan>'
            for i, line in enumerate(lines)
        )
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" '
            f'font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}">{spans}</text>'
        )

    def box(self, x, y, width, height, title, lines=(), color=INK, fill="white", dash=False):
        self.rect(x, y, width, height, fill=fill, stroke=color, dash=dash)
        self.text(x + 20, y + 32, title, size=22, color=color, weight=700)
        self.text(x + 20, y + 61, lines, size=18, color=MUTED, gap=25)

    def arrow(self, path, kind="control", both=False, dash=False):
        color = {"control": INK, "evidence": GREEN, "review": PURPLE}[kind]
        start = f' marker-start="url(#{kind})"' if both else ""
        dashed = ' stroke-dasharray="7 5"' if dash else ""
        self.parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" marker-end="url(#{kind})"{start}{dashed}/>'
        )

    def save(self):
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text("\n".join([*self.parts, "</svg>"]) + "\n", encoding="utf-8")


def main():
    f = Figure()
    f.text(40, 42, "CLICKCLICK", size=27, weight=700)
    f.text(247, 42, "Model, harness and persistent Session", size=24, color=MUTED)
    f.text(40, 76, "Context management in the harness  ·  durable records in Session  ·  device tools execute and observe", size=18)

    f.box(40, 105, 1040, 90, "Model", [
        "Interpret task and evidence · reason · choose actions · judge outcomes",
    ], color=PURPLE, fill="#F4EEF7")
    f.box(1140, 105, 420, 90, "Console", ["Tasks · devices · live view · trace inspection"])

    # Harness and Session have equal height and independent boundaries.
    f.rect(40, 255, 1040, 570, fill="#F7FAFE", stroke=BLUE)
    f.text(65, 292, "Agent harness", size=26, color=BLUE, weight=700)
    f.arrow("M 285 195 V 315", both=True)
    f.text(301, 228, "model I/O", size=17)
    f.arrow("M 1200 195 V 225 H 820 V 315", both=True)
    f.text(834, 216, "task / status", size=17)
    f.box(70, 315, 430, 85, "Model gateway", ["Role routing · provider protocol · call accounting"])
    f.arrow("M 500 357 H 570", both=True)

    f.box(70, 445, 430, 190, "Context, memory access and skills", [
        "Task + evidence · retrieve Session history",
        "Context projection · compaction · note tools",
        "App / workflow selection · role delivery",
        "Source references · skill version checks",
    ], color=GREEN, fill="#EEF7F3")
    f.arrow("M 500 485 H 570", kind="evidence")
    f.text(535, 463, "context", size=14, color=GREEN, anchor="middle")

    f.rect(570, 315, 480, 320, fill="#EDF3FA", stroke=BLUE)
    f.text(590, 346, "Agent loop and role orchestration", size=22, color=BLUE, weight=700)
    f.text(590, 373, "Role prompts / schemas · task and stage state", size=18, color=MUTED)
    f.rect(590, 401, 180, 60, stroke=BLUE)
    f.text(680, 438, "Planner", size=21, weight=700, anchor="middle")
    f.rect(855, 401, 175, 60, stroke=BLUE)
    f.text(942, 438, "Executor", size=21, weight=700, anchor="middle")
    f.arrow("M 770 422 H 855")
    f.arrow("M 855 446 H 770")
    f.text(812, 396, "execute", size=14, anchor="middle")
    f.rect(675, 511, 270, 56, fill="#F4EEF7", stroke=PURPLE, dash=True)
    f.text(810, 546, "Reviewer · on demand", size=20, color=PURPLE, weight=700, anchor="middle")
    f.arrow("M 680 461 V 485 H 735 V 511", kind="review", dash=True)
    f.arrow("M 942 461 V 485 H 885 V 511", kind="review", dash=True)
    f.text(590, 594, "Feedback → continue / replan / review / complete", size=18)

    f.box(70, 675, 980, 115, "Tools and execution control", [
        "Tool / read scope · schema validation · observation / target binding · action dispatch",
        "Focus / input verification · budgets · cancellation · error feedback · repeat guards",
    ])
    f.arrow("M 810 635 V 675", both=True)
    f.text(827, 660, "tools / feedback", size=16)
    f.text(70, 813, "Read Session to build context; record events, evidence and state updates through the store.", size=17, color=MUTED)

    # Session is a first-class column, not a small auxiliary store.
    f.rect(1140, 255, 420, 570, fill="#EEF7F3", stroke=GREEN)
    f.text(1165, 292, "Session", size=28, color=GREEN, weight=700)
    f.text(1165, 321, ["Persistent records, independent of", "the active model context window"], size=19, color=MUTED, gap=25)
    f.box(1170, 375, 360, 95, "History and events", ["Model / tool calls · action receipts"], color=GREEN)
    f.box(1170, 500, 360, 95, "Task state and notes", ["Stage records · versioned notes"], color=GREEN)
    f.box(1170, 625, 360, 95, "Observations and artifacts", ["Screenshots · source references"], color=GREEN)
    f.text(1170, 766, "SQLite records + artifact files", size=20, color=GREEN, weight=700)
    f.text(1170, 796, "Shared by runtime, Console and audits", size=17, color=MUTED)
    f.arrow("M 1140 485 H 1080", kind="evidence")
    f.text(1110, 471, "read", size=15, color=GREEN, anchor="middle")
    f.arrow("M 1080 740 H 1140", kind="evidence")
    f.text(1110, 761, "record", size=15, color=GREEN, anchor="middle")
    f.arrow("M 1350 255 V 195", kind="evidence")
    f.text(1366, 228, "read traces", size=16, color=GREEN)

    f.rect(40, 890, 1520, 190, fill="#F5F9F7", stroke=GREEN)
    f.text(65, 923, "Device tools and environment", size=24, color=GREEN, weight=700)
    f.box(80, 945, 430, 110, "Capture and perception", [
        "scrcpy · Accessibility Collector · ADB",
        "Pixels / tree / events → observation + SoM",
    ], color=GREEN)
    f.box(570, 945, 490, 110, "Android device / emulator", [
        "Apps and OS · changing UI state",
        "Execution environment",
    ], fill="#EFF1F5")
    f.box(1120, 945, 400, 110, "Device driver", ["ADB actions · IME input", "Local or remote host"])
    f.arrow("M 570 1000 H 510", kind="evidence")
    f.arrow("M 1120 1000 H 1060")
    f.arrow("M 480 945 V 825", kind="evidence")
    f.text(496, 851, "observations", size=17, color=GREEN)
    f.arrow("M 1000 825 V 855 H 1300 V 945")
    f.text(1030, 846, "actions", size=17)
    f.arrow("M 1450 945 V 872 H 900 V 825", kind="evidence")
    f.text(1466, 858, "receipts", size=17, color=GREEN)

    f.rect(40, 1130, 1520, 80, fill="#F5F6F8")
    f.text(65, 1163, "Evaluation", size=24, weight=700)
    f.text(300, 1163, "Controlled setup + official oracle · trace audit · accuracy / reliability / latency / cost", size=19)
    f.text(300, 1191, "Record fidelity · preservation of unrelated data · task completion · resource use", size=18, color=MUTED)
    f.arrow("M 1560 550 H 1580 V 1170 H 1560", kind="evidence")
    f.arrow("M 815 1130 V 1080")
    f.text(829, 1111, "fixture setup / scoring", size=16)

    f.arrow("M 44 1244 H 99")
    f.text(112, 1250, "Control / action", size=17)
    f.arrow("M 335 1244 H 390", kind="evidence")
    f.text(402, 1250, "Evidence / records", size=17, color=GREEN)
    f.arrow("M 675 1244 H 730", kind="review", dash=True)
    f.text(742, 1250, "Optional review", size=17, color=PURPLE)
    f.text(1560, 1250, "Logical modules, not separate processes", size=17, anchor="end", color=MUTED)
    f.save()
    print(OUTPUT)


if __name__ == "__main__":
    main()
