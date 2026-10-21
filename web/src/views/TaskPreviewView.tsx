import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { startTaskPreview } from "@/lib/taskPreview";
import type { PreviewView } from "@/lib/taskPreview";
import { ArrowUpRight, Eye, Smartphone } from "lucide-react";
import { PreviewModelLog } from "@/components/PreviewModelLog";
import { loadPreviewLocale, savePreviewLocale } from "@/lib/previewLocale";
import type { PreviewLocale } from "@/lib/previewLocale";

const taskLabels: Record<string, [string, string]> = {
  queued: ["排队中", "Queued"], running: ["执行中", "Running"],
  pausing: ["正在暂停", "Pausing"], paused: ["已暂停", "Paused"],
  waiting_for_input: ["等待确认", "Waiting for input"], succeeded: ["已完成", "Completed"],
  failed: ["任务未完成", "Task failed"], cancelled: ["已取消", "Cancelled"],
};

export function TaskPreviewView() {
  const { id = "" } = useParams();
  const [locale, setLocale] = useState<PreviewLocale>(loadPreviewLocale);
  const zh = locale === "zh-CN";
  const chooseLocale = (next: "en" | "zh-CN") => {
    setLocale(next);
    savePreviewLocale(next);
  };
  const [view, setView] = useState<PreviewView>({ state: null, frame: null,
    imageUrl: null, live: false, error: null, ageMs: 0 });
  useEffect(() => startTaskPreview(id, setView), [id]);
  const status = view.state?.task_status;
  const taskLabel = taskLabels[status ?? ""]?.[zh ? 0 : 1] ?? (zh ? "连接中" : "Connecting");
  const unsupported = (view.state?.reason ?? "").startsWith("unsupported_");
  const terminal = ["succeeded", "failed", "cancelled"].includes(status ?? "");
  const label = view.live ? (zh ? "画面可用" : "Preview available")
    : view.imageUrl ? (zh ? "最后画面" : "Last received frame") : (zh ? "等待画面" : "Waiting for preview");
  const empty = unsupported ? (zh ? "此任务暂不支持画面预览" : "Preview is unavailable for this task")
    : terminal ? (zh ? "任务已结束，可在详情回看" : "Task ended. View saved images in details")
      : status === "paused" ? (zh ? "任务已暂停" : "Task paused")
        : view.error ? (zh ? "画面暂不可用，正在重试" : "Preview unavailable. Retrying")
          : (zh ? "任务画面就绪后会显示在这里" : "The task screen will appear here when ready");
  const seconds = Math.max(0, Math.floor(view.ageMs/1000));
  const age = seconds < 60 ? (zh ? `${seconds}秒前` : `${seconds}s ago`)
    : (zh ? `${Math.floor(seconds/60)}分钟前` : `${Math.floor(seconds/60)}m ago`);
  return <div className="h-dvh min-h-[280px] w-full bg-bg-base text-text">
    <main lang={locale} className="mx-auto flex h-full w-full max-w-[1440px] min-w-0 flex-col gap-3 p-3 sm:gap-4 sm:p-5">
      <header className="flex min-w-0 shrink-0 flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="hidden rounded-xl border border-neon/20 bg-neon/10 p-2 text-neon sm:block">
            <Smartphone className="h-5 w-5" aria-hidden /></div>
          <div className="min-w-0">
            <h1 className="text-sm font-semibold tracking-tight sm:text-lg">ClickClick <span className="font-normal text-text-mute">/</span> {zh ? "任务预览" : "Task preview"}</h1>
            <p className="mt-0.5 text-[10px] text-text-mute sm:text-xs">#{id.slice(0, 8)} <span className="px-1">·</span> {taskLabel}</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-border px-2.5 py-1 text-[10px] text-text-mute sm:text-xs">
            <Eye className="h-3 w-3" aria-hidden />{zh ? "只读" : "Read only"}</span>
          <div className="flex rounded-lg border border-border bg-bg-1 p-0.5" role="group" aria-label={zh ? "界面语言" : "Interface language"}>
            {(["en", "zh-CN"] as const).map(language => <button key={language} type="button"
              aria-pressed={locale === language} onClick={() => chooseLocale(language)}
              className={`rounded-md px-2 py-1 text-[10px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan sm:text-xs ${locale === language ? "bg-bg-3 text-text" : "text-text-mute hover:text-text"}`}>
              {language === "en" ? "EN" : "中文"}</button>)}
          </div>
        </div>
      </header>
      <div className="grid min-h-0 min-w-0 flex-1 grid-cols-2 gap-2 sm:gap-4">
        <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-border bg-bg-1"
          aria-label={zh ? "手机画面" : "Phone screen"}>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-1 border-b border-border px-3 py-3">
            <h2 className="flex items-center gap-1.5 text-xs font-medium sm:text-sm">
              <span className={`h-1.5 w-1.5 rounded-full ${view.live ? "bg-neon" : "bg-text-faint"}`} />
              {zh ? "手机画面" : "Phone screen"}</h2>
            <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-text-mute">
              {view.imageUrl && !view.live && <span className="rounded-md border border-border px-2 py-1" role="status">{label}</span>}
              <span>{view.frame ? age : label}</span>
            </div>
          </div>
          <div className="flex min-h-0 flex-1 items-center justify-center bg-bg-base p-2 sm:p-4">
            {view.imageUrl ? <img src={view.imageUrl} alt={view.live
              ? (zh ? "任务最近的手机画面" : "Latest task screen") : (zh ? "任务最后收到的画面" : "Last received task screen")}
              className="block h-full max-h-full w-full min-w-0 rounded-lg object-contain" draggable={false} />
              : <div className="flex flex-col items-center gap-3 p-2 text-center text-text-mute">
                <Smartphone className="h-7 w-7 text-text-faint" aria-hidden />
                <p className="text-xs leading-relaxed">{empty}</p></div>}
          </div>
          <p className="shrink-0 border-t border-border px-3 py-2 text-[10px] leading-relaxed text-text-mute" role="status">
            {view.live ? (zh ? "最近画面，可能略有延迟" : "Latest screen; updates may be delayed")
              : view.imageUrl ? (zh ? "预览暂不可用，保留最后画面" : "Preview unavailable; showing the last frame") : label}</p>
        </section>
        <PreviewModelLog taskId={id} enabled={Boolean(view.state && !unsupported)} terminal={terminal} zh={zh} />
      </div>
      <footer className="flex shrink-0 items-center justify-between gap-2 text-[10px] text-text-mute sm:text-xs">
        <p>{zh ? "操作进度以任务详情为准" : "Check task details for execution progress"}</p>
        <a className="inline-flex shrink-0 items-center gap-1 text-cyan transition-colors hover:text-text" href={`/tasks/${encodeURIComponent(id)}`}>
          {zh ? "任务详情" : "Task details"}<ArrowUpRight className="h-3 w-3" aria-hidden /></a>
      </footer>
    </main>
  </div>;
}
