import { useEffect, useMemo, useRef } from "react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { cn, formatMs } from "@/lib/utils";
import { deriveCallRows } from "@/lib/expandRoleCalls";
import type { RoleCall } from "@/api/types";

interface RoundRowProps {
  label: string;
  role: "R" | "P" | "E";
  phase: string;
  selected: boolean;
  live: boolean;
  elapsedMs?: number | null;
  onSelect: () => void;
}

/**
 * timeline-round-rail (D3): the node is a 14px circle with a centered
 * `R` (Reviewer), `P` (Planner), or `E` (Executor) letter.
 * Selection adds `ring-2 ring-neon` to the node; the live step (latest
 * call while running) animates-pulse. The row gets a faint neon
 * background when selected.
 */
function RoundRow({
  label,
  role,
  phase,
  selected,
  live,
  elapsedMs,
  onSelect,
}: RoundRowProps) {
  const roleName = role === "R" ? "Reviewer" : role === "P" ? "Planner" : "Executor";

  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "group relative mx-auto grid w-[9rem] grid-cols-[2rem_1.25rem_2.5rem_1fr] items-center rounded font-mono transition-colors",
        selected ? "bg-neon/[0.06]" : "hover:bg-bg-2",
      )}
      title={`${roleName} · ${phase} · ${formatMs(elapsedMs)}`}
    >
      <span
        className={cn(
          "flex h-9 items-center justify-end pr-1 text-[11px] tabular-nums",
          selected
            ? "text-neon"
            : live
              ? "text-neon animate-pulse"
              : "text-text-mute",
        )}
      >
        {label}
      </span>

      <span className="relative flex h-9 items-center justify-center">
        <span
          className={cn(
            "relative z-10 flex h-[18px] w-[18px] items-center justify-center rounded-full font-mono text-[10px] font-bold leading-none",
            role === "R"
              ? "border border-amber bg-amber/20 text-amber"
              : role === "P"
                ? "bg-violet text-white"
                : "border border-cyan bg-cyan/20 text-cyan",
            selected && "ring-2 ring-neon",
            live && "animate-pulse",
          )}
        >
          {role}
        </span>
      </span>
      <span className="pl-1 text-left text-[9px] uppercase text-text-mute">
        {phase}
      </span>
      <span className="pr-1 text-right text-[8px] tabular-nums text-text-mute">
        {formatMs(elapsedMs)}
      </span>
    </button>
  );
}

export function Timeline({
  calls,
  selectedCallKey,
  onSelectCall,
  running,
  followLive,
  onFollowLiveChange,
}: {
  calls: RoleCall[];
  selectedCallKey: string | null;
  onSelectCall: (callKey: string) => void;
  running: boolean;
  followLive: boolean;
  onFollowLiveChange: (v: boolean) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastCallKey =
    calls.length > 0 ? calls[calls.length - 1].call_key : null;

  const rows = useMemo(() => deriveCallRows(calls), [calls]);

  useEffect(() => {
    if (!followLive || !running || !scrollRef.current) return;
    const el = scrollRef.current;
    el.scrollTop = el.scrollHeight;
  }, [rows.length, running, followLive]);

  return (
    <div className="flex h-full flex-col">
      <div className="relative flex h-8 items-center justify-center px-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-text-mute">
          Calls
        </span>
        <label
          className="absolute right-2 inline-flex cursor-pointer items-center gap-1"
          title={followLive ? "follow live on" : "follow live off"}
        >
          <input
            type="checkbox"
            checked={followLive}
            onChange={(e) => onFollowLiveChange(e.target.checked)}
            className="h-3 w-3 accent-neon"
          />
          <span className="font-mono text-[9px] uppercase tracking-wide text-text-mute">
            {followLive ? "on" : "off"}
          </span>
        </label>
      </div>

      <ScrollArea className="h-[calc(100vh-12rem)]">
        <div ref={scrollRef} className="relative">
          {rows.length > 0 && (
            <div
              aria-hidden
              className="pointer-events-none absolute top-0 bottom-0 z-0 w-px bg-border-hi"
              style={{ left: "3.625rem" }}
            />
          )}

          {rows.length === 0 ? (
            <div className="px-1 py-3 text-center font-mono text-[10px] text-text-mute">
              —
            </div>
          ) : (
            rows.map((row) => {
              const key = row.call.call_key;
              const selected = key === selectedCallKey;
              const live = running && key === lastCallKey;
              return (
                <RoundRow
                  key={key}
                  label={row.label}
                  role={row.role}
                  phase={row.phase}
                  selected={selected}
                  live={live}
                  elapsedMs={row.call.elapsed_ms}
                  onSelect={() => onSelectCall(key)}
                />
              );
            })
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
