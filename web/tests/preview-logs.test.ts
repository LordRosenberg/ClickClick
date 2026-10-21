import assert from "node:assert/strict";
import test from "node:test";
import type { TraceEvent } from "../src/api/types.ts";
import { appendModelOutput, emptyPreviewLogs, modelResponseText, startPreviewLogs,
  LOG_CHAR_LIMIT, LOG_LIMIT } from "../src/lib/previewLogs.ts";
import type { PreviewLogEnvironment, PreviewLogs } from "../src/lib/previewLogs.ts";
import { loadPreviewLocale, savePreviewLocale } from "../src/lib/previewLocale.ts";

const event = (text: string, sequence = 1, attempt = 1, round = "R"): TraceEvent => ({
  task_id: "T", kind: "agent_llm_stream", message: "", level: "INFO", ts: 100,
  payload: { text, sequence, attempt, round_id: round, invocation_id: "I", role: "executor" },
});
test("cumulative snapshots append only suffixes; retries and revisions preserve prior output", () => {
  let state = appendModelOutput(emptyPreviewLogs(), event("Hello"));
  state = appendModelOutput(state, event("Hello world", 2));
  assert.deepEqual(state.rows.map(r => r.text), ["Hello world"]);
  const current = state;
  assert.equal(appendModelOutput(state, event("late", 1)), current);
  state = appendModelOutput(state, event("Retry", 1, 2));
  state = appendModelOutput(state, event("Revised", 2, 2));
  assert.deepEqual(state.rows.map(r => r.text), ["Hello world", "Retry", "Revised"]);
  state = appendModelOutput(state, event("", 0, 2), "Revised final");
  assert.equal(state.rows.at(-1)?.text, " final");
  assert.equal(appendModelOutput(state, event("too late", 99, 2)), state);
});
test("saved projection exposes returned text/tool calls without debug or request metadata", () => {
  const text = modelResponseText({ content_blocks: [{ type: "text", text: "<script>literal</script>" },
    { type: "image", data: "BASE64" }], tool_calls: [{ name: "tap", arguments: { index: 3 }, id: "DEBUG_ID" }],
    private_reasoning: "SECRET", reasoning: { summary: "PRIVATE" }, request: "INPUT", usage: "DEBUG" });
  assert.match(text, /<script>literal<\/script>/);
  assert.match(text, /"name": "tap"/);
  assert.doesNotMatch(text, /SECRET|PRIVATE|BASE64|DEBUG|INPUT/);
  assert.equal(modelResponseText({ usage: "debug-only" }), "");
});
test("append buffers and dedup cursors remain bounded", () => {
  let state = emptyPreviewLogs();
  for (let i = 0; i < 400; i++) state = appendModelOutput(state, event("x".repeat(3000), 1, 1, `R${i}`));
  assert.ok(state.trimmed && state.rows.length <= LOG_LIMIT && state.cursors.size <= 128);
  assert.ok(state.rows.reduce((n, row) => n+row.text.length, 0) <= LOG_CHAR_LIMIT);
  state = appendModelOutput(state, event("y".repeat(100000), 1, 1, "large"));
  assert.equal(state.rows.at(-1)?.text.length, 16384);
});

function harness() {
  type Stream = ReturnType<PreviewLogEnvironment["open"]> & { closed: boolean };
  const streams: Stream[] = [], timers: { fn: () => void; ms: number }[] = [];
  const reads: { signal: AbortSignal; url: string; resolve: (r: Response) => void }[] = [];
  const views: PreviewLogs[] = [];
  let hidden = false, terminal = false, visibility = () => {}, subscribed = true;
  const env: PreviewLogEnvironment = {
    open: () => {
      const source: Stream = { onopen: null, onmessage: null, onerror: null, closed: false,
        close() { this.closed = true; } };
      streams.push(source); return source;
    },
    fetch: ((url, options) => new Promise<Response>(resolve => reads.push({
      signal: options!.signal!, url: String(url), resolve,
    }))) as typeof fetch,
    hidden: () => hidden, terminal: () => terminal,
    visibility: fn => { visibility = fn; return () => { subscribed = false; }; },
    later: (fn, ms) => { const timer = { fn, ms }; timers.push(timer); return timer; },
    cancel: timer => { const i = timers.indexOf(timer as typeof timers[0]); if (i >= 0) timers.splice(i, 1); },
  };
  const stop = startPreviewLogs("T", s => views.push(s), env);
  return { streams, timers, reads, views, stop,
    get subscribed() { return subscribed; },
    message(value: TraceEvent) { streams.at(-1)!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) })); },
    render() { const i = timers.findIndex(t => t.ms === 100); if (i >= 0) timers.splice(i, 1)[0].fn(); },
    hide(value: boolean) { hidden = value; visibility(); },
    terminal() { terminal = true; },
  };
}
test("cross-task/late messages are ignored; hidden, terminal and unmount release subscriptions", () => {
  const h = harness();
  h.message({ ...event("wrong"), task_id: "OTHER" }); h.render();
  assert.equal(h.views.at(-1)?.rows.length, 0);
  h.message(event("first")); h.render();
  const old = h.streams[0];
  h.hide(true); assert.ok(old.closed);
  old.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event("late", 2)) }));
  h.hide(false); h.message(event("first")); h.render();
  assert.deepEqual(h.views.at(-1)?.rows.map(r => r.text), ["first"]);
  h.terminal(); h.streams.at(-1)!.onerror?.(new Event("error")); h.render();
  assert.equal(h.views.at(-1)?.connection, "closed");
  assert.equal(h.timers.length, 0);
  h.stop(); assert.equal(h.subscribed, false);
});
test("saved-response reads are task scoped, single-flight and aborted on unmount", () => {
  const h = harness();
  for (let i = 0; i < 100; i++) h.message({ ...event("", i, 1, `R${i}`), kind: "agent_llm_round_finished",
    payload: { round_id: `R${i}`, invocation_id: "I", response_ref: `llm/${i}.json` } });
  assert.equal(h.reads.length, 1);
  assert.equal(h.reads[0].url, "/api/tasks/T/artifacts/llm/0.json");
  h.stop(); assert.equal(h.reads[0].signal.aborted, true);
  assert.ok(h.streams[0].closed);
  assert.equal(h.timers.length, 0);
});

test("identical saved responses from different rounds remain separate output", async () => {
  const h = harness();
  for (const round of ["A", "B"]) h.message({ ...event("", 1, 1, round), kind: "agent_llm_round_finished",
    payload: { round_id: round, invocation_id: "I", role: "planner", response_ref: "llm/same.json" } });
  h.reads[0].resolve(Response.json({ content: "same output" }));
  for (let i = 0; i < 6; i++) await new Promise(setImmediate);
  assert.equal(h.reads.length, 2);
  h.reads[1].resolve(Response.json({ content: "same output" }));
  for (let i = 0; i < 6; i++) await new Promise(setImmediate);
  h.render();
  assert.deepEqual(h.views.at(-1)?.rows.map(row => row.text), ["same output", "same output"]);
  h.stop();
});

test("response references cannot address setup or another API; oversized output is cancelled", async () => {
  const h = harness();
  h.message({ ...event(""), kind: "agent_llm_round_finished", payload: {
    round_id: "R", invocation_id: "I", response_ref: "llm/../../setup/access" } });
  assert.equal(h.reads.length, 0);
  h.message({ ...event(""), kind: "agent_llm_round_finished", payload: {
    round_id: "R", invocation_id: "I", response_ref: "llm/large.json" } });
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(600000)); },
    cancel() { cancelled = true; },
  });
  h.reads[0].resolve(new Response(body));
  for (let i = 0; i < 6; i++) await new Promise(setImmediate);
  h.render();
  assert.ok(cancelled && h.views.at(-1)?.trimmed);
  assert.equal(h.views.at(-1)?.rows.length, 0);
  h.stop();
});

test("preview defaults to English, persists a choice and isolates unavailable/global storage", () => {
  const values = new Map([["clickclick.locale", "zh-CN"]]);
  const storage = () => ({ getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); } });
  assert.equal(loadPreviewLocale(storage), "en");
  savePreviewLocale("zh-CN", storage);
  assert.equal(loadPreviewLocale(storage), "zh-CN");
  savePreviewLocale("en", storage);
  assert.equal(loadPreviewLocale(storage), "en");
  assert.equal(values.get("clickclick.locale"), "zh-CN");
  const unavailable = () => { throw new Error("storage disabled"); };
  assert.equal(loadPreviewLocale(unavailable), "en");
  assert.doesNotThrow(() => savePreviewLocale("zh-CN", unavailable));
});

test("a terminal status arriving during reconnect backoff prevents another subscription", () => {
  const h = harness();
  h.streams[0].onerror?.(new Event("error"));
  h.terminal();
  const timer = h.timers.findIndex(t => t.ms === 1000);
  h.timers.splice(timer, 1)[0].fn(); h.render();
  assert.equal(h.streams.length, 1);
  assert.equal(h.views.at(-1)?.connection, "closed");
  h.stop();
});
