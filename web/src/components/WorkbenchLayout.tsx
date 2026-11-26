import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { WorkbenchTask } from "@/api/client";
import { WorkbenchScreen } from "@/components/WorkbenchScreen";
import { t } from "@/lib/locale";
import { clampScreenWidth, suggestedScreenWidth } from "@/lib/workbenchFeed";

function isDesktop(device: string | undefined): boolean {
  return /(?:windows|linux|macos|desktop)/i.test(device ?? "");
}

export function WorkbenchLayout({ execution, composer, task, device }: {
  execution: ReactNode; composer: ReactNode; task?: WorkbenchTask; device?: string;
}) {
  const layout = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [aspect, setAspect] = useState<number>();
  const [manualWidth, setManualWidth] = useState<number>();
  const [expanded, setExpanded] = useState(false);
  const [tab, setTab] = useState<"flow" | "screen">("flow");
  const desktop = isDesktop(device);
  const shape = aspect ?? (desktop ? 16 / 9 : 9 / 19.5);
  const preferred = suggestedScreenWidth(width, shape, expanded);
  const screenWidth = manualWidth !== undefined && !expanded ? clampScreenWidth(width, manualWidth, false) : preferred;

  useEffect(() => {
    const element = layout.current;
    if (!element) return;
    const resize = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    resize.observe(element);
    return () => resize.disconnect();
  }, []);

  return <>
    <div className="wb-tabs" role="group" aria-label={t("工作台视图")}>
      <button type="button" className="wb-button" aria-pressed={tab === "flow"} onClick={() => setTab("flow")}>{t("执行流")}</button>
      <button type="button" className="wb-button" aria-pressed={tab === "screen"} onClick={() => setTab("screen")}>{t("设备画面")}</button>
    </div>
    <div ref={layout} className="wb-layout" data-tab={tab} style={{ "--screen-width": width ? `${screenWidth}px` : "36%" } as CSSProperties}>
      <section className="wb-execution" aria-label={t("执行流")}>{execution}</section>
      <div className="wb-divider" role="separator" aria-label={t("调整设备画面宽度")} aria-orientation="vertical"
        aria-valuenow={screenWidth} aria-valuemin={280} aria-valuemax={clampScreenWidth(width, width, expanded)} tabIndex={0}
        onDoubleClick={() => { setManualWidth(undefined); setExpanded(false); }}
        onPointerDown={event => {
          event.preventDefault();
          event.currentTarget.focus();
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={event => {
          if (!event.currentTarget.hasPointerCapture(event.pointerId) || !layout.current) return;
          const desired = layout.current.getBoundingClientRect().right - event.clientX;
          setExpanded(false);
          setManualWidth(clampScreenWidth(width, desired, false));
        }}
        onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
          event.preventDefault();
          setExpanded(false);
          setManualWidth(event.key === "Home" ? undefined :
            clampScreenWidth(width, screenWidth + (event.key === "ArrowLeft" ? 24 : -24), false));
        }}><span /></div>
      <WorkbenchScreen task={task} device={device} expanded={expanded}
        onExpand={() => setExpanded(previous => !previous)} onAspect={setAspect} />
      <div className="wb-composer">{composer}</div>
    </div>
  </>;
}
