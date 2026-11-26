import type { TraceEvent } from "../api/types";

type DatedRow = { id: string; ts: number };
export type ThinkingRow = DatedRow & {
  kind: "thinking"; role: string; text: string; model: string;
  phase: "started" | "streaming" | "finished"; attempt: number; sequence: number; toolCount: number;
};
export type ToolRow = DatedRow & {
  kind: "tool" | "action"; name: string; status: "running" | "succeeded" | "failed";
  duration?: number; text?: string;
};
export type FeedRow = ThinkingRow | ToolRow | (DatedRow & { kind: "human"; outcome: string })
  | (DatedRow & { kind: "lifecycle"; event: string });

const text = (value: unknown) => typeof value === "string" ? value : "";
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : 0;

export function mergeWorkbenchEvents(previous: Map<string, FeedRow>, events: TraceEvent[]): Map<string, FeedRow> {
  const rows = new Map(previous);
  for (const event of events) {
    const p = event.payload;
    const ts = number(event.ts);
    if (event.kind === "agent_llm_round_started" || event.kind === "agent_llm_stream" || event.kind === "agent_llm_round_finished") {
      const round = text(p.round_id);
      if (!round) continue;
      const id = `round:${round}`;
      const before = rows.get(id) as ThinkingRow | undefined;
      const phase = event.kind === "agent_llm_round_started" ? "started" :
        event.kind === "agent_llm_round_finished" ? "finished" : "streaming";
      const attempt = number(p.attempt);
      const sequence = number(p.sequence);
      if (before && (phase === "started" || (phase === "streaming" &&
        (before.phase === "finished" || attempt < before.attempt ||
          (attempt === before.attempt && sequence <= before.sequence))))) {
        if (ts < before.ts && phase === "started") rows.set(id, { ...before, ts });
        continue;
      }
      const sameAttempt = !before || !attempt || attempt === before.attempt;
      rows.set(id, {
        id, kind: "thinking", ts: Math.min(before?.ts ?? ts, ts), phase, attempt, sequence,
        role: text(p.role) || before?.role || "",
        text: text(p.progress_text) || (sameAttempt ? before?.text : "") || "",
        model: text(p.model) || before?.model || "",
        toolCount: number(p.tool_call_count) || (sameAttempt ? before?.toolCount : 0) || 0,
      });
    } else if (event.kind === "agent_tool_started" || event.kind === "agent_tool_finished" || event.kind === "agent_tool_failed") {
      const call = text(p.call_id);
      if (!call || !p.name) continue;
      const id = `tool:${text(p.invocation_id)}:${call}`;
      const before = rows.get(id) as ToolRow | undefined;
      const running = event.kind === "agent_tool_started";
      if (running && before) {
        if (ts < before.ts) rows.set(id, { ...before, ts });
        continue;
      }
      rows.set(id, {
        id, ts: Math.min(before?.ts ?? ts, ts), kind: "tool", name: text(p.name),
        status: running ? "running" : p.status === "succeeded" ? "succeeded" : "failed",
        duration: number(p.elapsed_ms),
      });
    } else if (event.kind === "executor_tick" && (p.action_type || p.progress_text)) {
      const id = `action:${event.step_seq}:${ts}`;
      rows.set(id, {
        id, ts, kind: "action", name: text(p.action_type), text: text(p.progress_text),
        status: p.success === true ? "succeeded" : "failed",
      });
    } else if (event.kind === "system" && p.interaction_id) {
      const id = `human:${text(p.interaction_id)}`;
      rows.set(id, { id, ts, kind: "human", outcome: text(p.outcome) });
    } else if (event.kind === "system" && typeof p.event === "string") {
      const id = `lifecycle:${p.event}:${ts}`;
      rows.set(id, { id, ts, kind: "lifecycle", event: p.event });
    }
  }
  return rows;
}

export function suggestedScreenWidth(total: number, aspect: number, expanded: boolean): number {
  const preferred = expanded ? total * 0.72 :
    aspect < 0.85 ? 360 : aspect < 1.4 ? total * 0.44 : total * 0.56;
  return clampScreenWidth(total, preferred, expanded);
}

export function clampScreenWidth(total: number, width: number, expanded: boolean): number {
  const minExecution = expanded ? 350 : 440;
  return Math.max(280, Math.min(width, total - minExecution - 8));
}
