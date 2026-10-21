import { useEffect, useRef, useState } from "react";
import { MessageSquareText } from "lucide-react";
import { emptyPreviewLogs, startPreviewLogs } from "@/lib/previewLogs";

const roles: Record<string, [string, string]> = {
  planner: ["规划", "Planner"], executor: ["执行", "Executor"], reviewer: ["检查", "Reviewer"],
};

export function PreviewModelLog({ taskId, enabled, terminal, zh }: {
  taskId: string; enabled: boolean; terminal: boolean; zh: boolean;
}) {
  const [logs, setLogs] = useState(emptyPreviewLogs);
  const scroller = useRef<HTMLDivElement>(null), follow = useRef(true);
  const terminalRef = useRef(terminal);
  terminalRef.current = terminal;
  useEffect(() => {
    setLogs(emptyPreviewLogs()); follow.current = true;
    if (!enabled) return;
    return startPreviewLogs(taskId, setLogs, {
      open: url => new EventSource(url), fetch: (url, options) => fetch(url, options),
      hidden: () => document.hidden, terminal: () => terminalRef.current,
      visibility: fn => {
        document.addEventListener("visibilitychange", fn);
        return () => document.removeEventListener("visibilitychange", fn);
      },
      later: (fn, ms) => window.setTimeout(fn, ms),
      cancel: timer => window.clearTimeout(timer as number),
    });
  }, [taskId, enabled]);
  useEffect(() => {
    if (follow.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [logs.rows]);
  const connection = !enabled ? (zh ? "暂不可用" : "Unavailable")
    : logs.connection === "open" ? (zh ? "实时追加" : "Appending live")
      : logs.connection === "closed" ? (zh ? "输出已结束" : "Output ended")
        : logs.connection === "retrying" ? (zh ? "正在重连" : "Reconnecting")
          : (zh ? "正在连接" : "Connecting");
  return <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-border bg-bg-1"
    aria-label={zh ? "模型输出日志" : "Model output log"}>
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-1 border-b border-border px-3 py-3">
      <h2 className="flex items-center gap-1.5 text-xs font-medium sm:text-sm">
        <MessageSquareText className="hidden h-4 w-4 text-violet sm:block" aria-hidden />
        {zh ? "模型输出" : "Model output"}
      </h2>
      <span className="text-[10px] text-text-mute" role="status">{connection}</span>
    </div>
    <div ref={scroller} tabIndex={0} aria-label={zh ? "模型日志内容" : "Model log content"}
      className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-4 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-violet/50 sm:px-4"
      onScroll={e => {
        const el = e.currentTarget;
        follow.current = el.scrollHeight-el.scrollTop-el.clientHeight < 48;
      }}>
      {logs.trimmed && <p className="mb-4 text-[11px] leading-relaxed text-text-mute">
        {zh ? "显示最近输出，完整记录可在任务详情查看。" : "Recent output shown. Full records are in task details."}</p>}
      {!logs.rows.length && <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <MessageSquareText className="h-6 w-6 text-text-faint" aria-hidden />
        <p className="text-xs text-text-mute">{zh ? "等待模型输出" : "Waiting for model output"}</p>
        <p className="max-w-52 text-[11px] leading-relaxed text-text-mute">
          {zh ? "任务中的新输出会依次追加在这里。" : "New output from this task will appear here in order."}</p>
      </div>}
      <div className="space-y-4" aria-live="off">
        {logs.rows.map(row => <article key={row.key} className="min-w-0 border-l border-violet/30 pl-2.5 sm:pl-3">
          <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-text-mute">
            <span className="font-medium text-violet">{roles[row.role]?.[zh ? 0 : 1] ?? (zh ? "模型" : "Model")}</span>
            <time dateTime={new Date(row.ts*1000).toISOString()}>{new Date(row.ts*1000).toLocaleTimeString(
              zh ? "zh-CN" : "en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
            {row.saved && <span>{zh ? "完整输出" : "Saved output"}</span>}
          </div>
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-6 text-text [overflow-wrap:anywhere] sm:text-xs">
            {row.text}</pre>
        </article>)}
      </div>
    </div>
  </section>;
}
