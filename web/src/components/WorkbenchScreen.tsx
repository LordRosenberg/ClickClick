import { useEffect, useState } from "react";
import { Maximize2, Minimize2, Monitor } from "lucide-react";
import { artifactUrl, type WorkbenchTask } from "@/api/client";
import { t } from "@/lib/locale";
import { taskIsNonterminal } from "@/lib/taskLifecycle";
import { startTaskPreview, type PreviewView } from "@/lib/taskPreview";

export function WorkbenchScreen({ task, device, expanded, onExpand, onAspect }: {
  task?: WorkbenchTask; device?: string;
  expanded: boolean; onExpand: () => void; onAspect: (aspect: number) => void;
}) {
  const [preview, setPreview] = useState<PreviewView>();
  const [broken, setBroken] = useState<string>();
  const [actualSize, setActualSize] = useState(false);
  const enabled = task?.preview_supported && taskIsNonterminal(task.status);
  const id = task?.id;
  useEffect(() => {
    if (id && enabled) return startTaskPreview(id, setPreview);
  }, [id, enabled]);

  const last = enabled ? preview?.imageUrl : null;
  const saved = task?.screenshot?.ref && artifactUrl(task.screenshot.ref, id);
  const live = Boolean(last && preview?.live && preview.ageMs < 2500);
  const frame = last && last !== broken ? last : saved !== broken ? saved : null;
  const screenStatus = frame && frame === last
    ? live ? t("实时画面") : t("最后一帧")
    : frame ? t("最近截图")
    : !task ? t("派发后显示设备画面")
    : enabled ? t(preview?.error ? "画面暂时不可用" : "等待设备画面")
    : t("暂无任务截图");
  const timestamp = frame && (frame === last
    ? t("{value0} 秒前更新", { value0: Math.floor((preview?.ageMs ?? 0) / 1000) })
    : task?.screenshot?.recorded_at
      ? new Date(task.screenshot.recorded_at * 1000).toLocaleTimeString()
      : null);

  return <section className="wb-screen" aria-label={t("设备画面")}>
    <header className="wb-screen-header">
      <div className="min-w-0">
        <h2 className="truncate text-sm font-semibold">{t("设备画面")}{device && <span className="ml-2 font-normal text-text-mute">{device}</span>}</h2>
        <p className="mt-1 flex items-center gap-2 text-xs text-text-mute">
          <span className={live && frame === last ? "wb-screen-dot is-live" : "wb-screen-dot"} />
          {screenStatus}
          {timestamp && <span>· {timestamp}</span>}
        </p>
      </div>
      <div className="flex items-center gap-1">
        <button type="button" className="wb-button" disabled={!frame} aria-pressed={!actualSize}
          onClick={() => setActualSize(false)}>{t("适应")}</button>
        <button type="button" className="wb-button" disabled={!frame} aria-pressed={actualSize}
          onClick={() => setActualSize(true)}>100%</button>
        <button type="button" className="wb-button wb-expand" aria-pressed={expanded} onClick={onExpand}
          title={t(expanded ? "还原画面大小" : "放大画面")} aria-label={t(expanded ? "还原画面大小" : "放大画面")}>
          {expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
      </div>
    </header>
    <div className={actualSize && frame ? "wb-screen-image actual-size" : "wb-screen-image"} tabIndex={actualSize && frame ? 0 : undefined}>
      {frame ? <img src={frame} alt={screenStatus} draggable={false}
        onLoad={event => {
          const image = event.currentTarget;
          if (image.naturalHeight) onAspect(image.naturalWidth / image.naturalHeight);
        }} onError={() => setBroken(frame)} />
        : <div className="wb-screen-empty"><Monitor size={28} aria-hidden />{screenStatus}</div>}
    </div>
  </section>;
}
