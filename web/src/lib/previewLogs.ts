import type { TraceEvent } from "../api/types.ts";

export const LOG_LIMIT = 200;
export const LOG_CHAR_LIMIT = 262144;
const BLOCK_LIMIT = 16384;
interface Cursor { attempt: number; sequence: number; length: number; tail: string; saved: boolean }
export interface PreviewLogRow {
  key: number; round: string; role: string; text: string; ts: number; saved: boolean;
}
export interface PreviewLogs {
  rows: PreviewLogRow[]; cursors: Map<string, Cursor>; nextKey: number;
  trimmed: boolean; connection: "connecting" | "open" | "retrying" | "closed";
}
export const emptyPreviewLogs = (): PreviewLogs => ({
  rows: [], cursors: new Map(), nextKey: 0, trimmed: false, connection: "connecting",
});

/** Stream text is cumulative. Preserve earlier output and append only a suffix. */
export function appendModelOutput(state: PreviewLogs, event: TraceEvent, savedText?: string): PreviewLogs {
  if (event.kind !== "agent_llm_stream" && savedText === undefined) return state;
  const p = event.payload;
  if (typeof p.round_id !== "string" || p.round_id.length > 512 ||
      typeof p.invocation_id !== "string" || p.invocation_id.length > 256) return state;
  const key = `${p.invocation_id}:${p.round_id}`;
  const current = state.cursors.get(key);
  const saved = savedText !== undefined;
  const text = savedText ?? (typeof p.text === "string" ? p.text : "");
  const attempt = Number.isSafeInteger(p.attempt) ? Number(p.attempt) : current?.attempt ?? 1;
  const sequence = saved ? Number.MAX_SAFE_INTEGER : Number(p.sequence ?? 0);
  if (!Number.isSafeInteger(sequence) || sequence < 0 || attempt < 1) return state;
  if (current && (current.saved || attempt < current.attempt ||
      (!saved && attempt === current.attempt && sequence <= current.sequence))) return state;
  let addition = text;
  let continuous = false;
  if (current && attempt === current.attempt && text.length >= current.length &&
      text.slice(current.length-current.tail.length, current.length) === current.tail) {
    addition = text.slice(current.length);
    continuous = true;
  }
  const cursors = new Map(state.cursors);
  cursors.delete(key);
  cursors.set(key, { attempt, sequence, length: text.length, tail: saved ? "" : text.slice(-BLOCK_LIMIT), saved });
  while (cursors.size > 128) cursors.delete(cursors.keys().next().value!);
  let rows = state.rows;
  let trimmed = state.trimmed || addition.length > BLOCK_LIMIT;
  let nextKey = state.nextKey;
  if (addition) {
    const round = `${key}:${attempt}`;
    const role = ["planner", "executor", "reviewer"].includes(String(p.role)) ? String(p.role) : "model";
    const ts = typeof event.ts === "number" && Number.isFinite(event.ts) && event.ts >= 0 &&
      event.ts <= 8640000000000 ? event.ts : Date.now()/1000;
    addition = addition.slice(-BLOCK_LIMIT);
    rows = [...rows];
    const last = rows.at(-1);
    if (continuous && last?.round === round && last.saved === saved && last.text.length+addition.length <= BLOCK_LIMIT) {
      rows[rows.length-1] = { ...last, text: last.text+addition };
    } else rows.push({ key: nextKey++, round, role, text: addition, ts, saved });
  }
  let characters = rows.reduce((sum, row) => sum+row.text.length, 0);
  while (rows.length > LOG_LIMIT || characters > LOG_CHAR_LIMIT) {
    characters -= rows[0].text.length;
    rows = rows.slice(1);
    trimmed = true;
  }
  return { ...state, rows, cursors, trimmed, nextKey };
}

/** Project only returned text/tool calls, never request or diagnostic metadata. */
export function modelResponseText(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const p = value as Record<string, unknown>;
  const blocks = Array.isArray(p.content_blocks) ? p.content_blocks : [];
  const parts = blocks.flatMap(block => block && typeof block === "object" &&
    block.type === "text" && typeof block.text === "string" ? [block.text] : []);
  if (!parts.length && typeof p.content === "string") parts.push(p.content);
  if (Array.isArray(p.tool_calls)) for (const call of p.tool_calls) {
    if (call && typeof call.name === "string") {
      parts.push(JSON.stringify({ name: call.name, arguments: call.arguments }, null, 2));
    }
  }
  return parts.join("\n");
}

interface StreamLike {
  onopen: ((event: Event) => unknown) | null;
  onmessage: ((event: MessageEvent) => unknown) | null;
  onerror: ((event: Event) => unknown) | null;
  close(): void;
}
export interface PreviewLogEnvironment {
  open(url: string): StreamLike;
  fetch: typeof fetch;
  hidden(): boolean;
  visibility(fn: () => void): () => void;
  later(fn: () => void, ms: number): unknown;
  cancel(timer: unknown): void;
  terminal(): boolean;
}

export function startPreviewLogs(taskId: string, update: (logs: PreviewLogs) => void,
  env: PreviewLogEnvironment): () => void {
  let state = emptyPreviewLogs(), closed = false, attempt = 0, generation = 0;
  let stream: StreamLike | null = null, reconnect: unknown, render: unknown;
  let activeRead: AbortController | null = null;
  let readDeadline: unknown;
  let completedEventTime = 0;
  const queue: { event: TraceEvent; ref: string; key: string }[] = [];
  const pending = new Set<string>(), loaded = new Set<string>();
  const emit = () => {
    if (render !== undefined || closed) return;
    render = env.later(() => { render = undefined; if (!closed) update(state); }, 100);
  };
  const connection = (value: PreviewLogs["connection"]) => {
    state = { ...state, connection: value }; emit();
  };
  async function readResponses() {
    if (activeRead || closed || env.hidden()) return;
    const next = queue.shift();
    if (!next) return;
    const controller = activeRead = new AbortController(), gen = generation;
    const deadline = readDeadline = env.later(() => controller.abort(), 10000);
    try {
      const path = next.ref.split("/").map(encodeURIComponent).join("/");
      const response = await env.fetch(`/api/tasks/${encodeURIComponent(taskId)}/artifacts/${path}`,
        { signal: controller.signal, cache: "no-store", credentials: "same-origin" });
      if (!response.ok || !response.body) throw new Error("output_unavailable");
      const reader = response.body.getReader();
      const parts: Uint8Array[] = []; let size = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          size += part.value.byteLength;
          if (size > 524288) {
            await reader.cancel(); state = { ...state, trimmed: true };
            throw new Error("output_limit");
          }
          parts.push(part.value);
        }
      } finally { reader.releaseLock(); }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
      const text = modelResponseText(JSON.parse(new TextDecoder().decode(bytes)));
      if (!closed && gen === generation && !env.hidden()) {
        state = appendModelOutput(state, next.event, text);
        loaded.add(next.key);
        while (loaded.size > 128) loaded.delete(loaded.values().next().value!);
        emit();
      }
    } catch {
      if (!closed && gen === generation) connection("retrying");
    } finally {
      env.cancel(deadline);
      pending.delete(next.key);
      if (activeRead === controller) { activeRead = null; readDeadline = undefined; }
      void readResponses();
    }
  }
  const connect = () => {
    if (closed || env.hidden()) return;
    if (env.terminal() && attempt > 0) { connection("closed"); return; }
    let source: StreamLike;
    try { source = stream = env.open(`/api/tasks/${encodeURIComponent(taskId)}/stream`); }
    catch {
      connection("retrying");
      reconnect = env.later(connect, [1000, 2000, 5000][Math.min(attempt++, 2)]);
      return;
    }
    source.onopen = () => { if (stream === source) { attempt = 0; connection("open"); } };
    source.onmessage = message => {
      if (closed || stream !== source || env.hidden()) return;
      let event: TraceEvent;
      try { event = JSON.parse(message.data); } catch { return; }
      if (event.task_id !== taskId || !event.payload || typeof event.payload !== "object") return;
      // Replayed history older than retained output is not appended again.
      if (state.trimmed && event.ts && state.rows[0] && event.ts < state.rows[0].ts) return;
      if (event.kind === "agent_llm_stream") {
        state = appendModelOutput(state, event); emit();
      } else if (event.kind === "agent_llm_round_finished") {
        const ref = event.payload.response_ref;
        const key = `${event.payload.invocation_id}:${event.payload.round_id}:${ref}`;
        if (typeof event.payload.round_id !== "string" || typeof event.payload.invocation_id !== "string" ||
            typeof ref !== "string" || !/^llm\/[A-Za-z0-9_-]+\.json$/.test(ref) ||
            ref.length > 512 || loaded.has(key) || pending.has(key)) return;
        if (typeof event.ts === "number" && Number.isFinite(event.ts)) {
          if (event.ts < completedEventTime) return;
          completedEventTime = Math.max(completedEventTime, event.ts);
        }
        if (queue.length >= 64) {
          pending.delete(queue.shift()!.key);
          state = { ...state, trimmed: true };
        }
        pending.add(key); queue.push({ event, ref, key }); void readResponses();
      }
    };
    source.onerror = () => {
      if (closed || stream !== source) return;
      source.close(); stream = null;
      if (env.terminal()) { connection("closed"); return; }
      connection("retrying");
      reconnect = env.later(connect, [1000, 2000, 5000][Math.min(attempt++, 2)]);
    };
  };
  const suspend = () => {
    generation++; stream?.close(); stream = null;
    env.cancel(reconnect); env.cancel(readDeadline); activeRead?.abort();
    queue.length = 0; pending.clear();
  };
  const unsubscribe = env.visibility(() => {
    suspend();
    if (!env.hidden()) { connection("connecting"); connect(); }
  });
  update(state); connect();
  return () => {
    closed = true; suspend(); unsubscribe(); env.cancel(render);
    loaded.clear(); state = emptyPreviewLogs();
  };
}
