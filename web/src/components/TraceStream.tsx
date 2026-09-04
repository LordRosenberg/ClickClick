import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { listTraces } from "@/api/client";
import type { LogLevel, TraceEvent } from "@/api/types";

const LEVELS: (LogLevel | "ALL")[] = ["ALL", "DEBUG", "INFO", "WARN", "ERROR"];

const levelColor: Record<LogLevel, string> = {
  DEBUG: "text-text-faint",
  INFO: "text-cyan",
  WARN: "text-amber",
  ERROR: "text-err",
};

const kindColor: Record<string, string> = {
  planner_decision: "border-violet/40 bg-violet/10 text-violet",
  reviewer_decision: "border-amber/40 bg-amber/10 text-amber",
  executor_tick: "border-cyan/40 bg-cyan/10 text-cyan",
  loop_tick: "border-amber/40 bg-amber/10 text-amber",
  llm: "border-border bg-bg-2 text-text-mute",
  ui: "border-border bg-bg-2 text-text-mute",
  shot: "border-border bg-bg-2 text-text-mute",
  dag: "border-border bg-bg-2 text-text-mute",
  skill: "border-border bg-bg-2 text-text-mute",
  system: "border-border bg-bg-2 text-text-mute",
};

function PayloadRow({ payload }: { payload: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const keys = Object.keys(payload);
  if (keys.length === 0) return null;
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="font-mono text-[10px] uppercase tracking-wider text-text-mute hover:text-text"
      >
        {open ? "▾ collapse payload" : "▸ expand payload"}
      </button>
      {open && (
        <pre className="mt-1 max-h-48 overflow-auto rounded border border-border bg-bg-2 p-2 font-mono text-[11px] text-text">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </div>
  );
}

export function TraceStream({ taskId }: { taskId: string }) {
  const [level, setLevel] = useState<LogLevel | "ALL">("ALL");

  const { data, isLoading } = useQuery({
    queryKey: ["traces", taskId, level],
    queryFn: () => listTraces(taskId, level === "ALL" ? undefined : level),
    refetchInterval: 2000,
  });

  const traces: TraceEvent[] = data ?? [];

  return (
    <div className="space-y-3 font-mono">
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider">
        {LEVELS.map((l) => (
          <button
            key={l}
            type="button"
            onClick={() => setLevel(l)}
            className={cn(
              "rounded border px-2.5 py-1 transition-colors",
              level === l
                ? "border-neon/40 bg-neon/10 text-neon"
                : "border-border bg-transparent text-text-mute hover:border-border-hi hover:text-text",
            )}
          >
            {l}
          </button>
        ))}
      </div>
      <ScrollArea className="h-[calc(100vh-16rem)]">
        <div className="space-y-1 pr-2 text-xs">
          {isLoading && (
            <div className="text-text-mute">loading traces…</div>
          )}
          {!isLoading && traces.length === 0 && (
            <div className="text-text-mute">— no trace events</div>
          )}
          {traces.map((e, i) => (
            <div
              key={i}
              className="rounded border border-border bg-bg-1 p-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge
                  variant="outline"
                  className={cn(
                    "font-mono text-[10px] uppercase tracking-wide",
                    kindColor[e.kind] ?? "border-border bg-bg-2 text-text-mute",
                  )}
                >
                  {e.kind}
                </Badge>
                <span className={cn("font-semibold uppercase", levelColor[e.level])}>
                  {e.level}
                </span>
                {e.step_seq != null && (
                  <span className="text-text-mute">
                    step {String(e.step_seq).padStart(2, "0")}
                  </span>
                )}
                {e.ts != null && (
                  <span className="ml-auto text-text-mute">
                    {new Date(e.ts * 1000).toLocaleTimeString()}
                  </span>
                )}
              </div>
              <div className="mt-1 text-text">{e.message}</div>
              <PayloadRow payload={e.payload ?? {}} />
            </div>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
