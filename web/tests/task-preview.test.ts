import assert from "node:assert/strict";
import test from "node:test";
import { parsePreviewFrame, startTaskPreview } from "../src/lib/taskPreview.ts";
import type { PreviewEnvironment, PreviewFrame, PreviewView } from "../src/lib/taskPreview.ts";

const frame = (id = 1, epoch = "run-1", generation = 1): PreviewFrame => ({
  task_id: "T", device_key: "S", run_epoch: epoch, provider_generation: generation,
  frame_id: id, width: 32, height: 64, geometry: { stream_width: 32, stream_height: 64 },
  frame_timestamp: 10, frame_age_ms: 20, observed_at: 11,
});
const state = (f = frame()) => Response.json({ task_id: "T", task_status: "running",
  preview_state: "available", reason: "", frame: f });
const pixels = (f = frame()) => new Response(new Blob(["png"]), { headers: {
  "Content-Type": "image/png", "X-ClickClick-Preview-Frame": JSON.stringify(f),
} });
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(setImmediate); };

function harness() {
  const requests: { url: string; signal: AbortSignal; resolve: (r: Response) => void }[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const revoked: string[] = [];
  const views: PreviewView[] = [];
  let hidden = false, visibility = () => {}, urls = 0, subscribed = true;
  const env: PreviewEnvironment = {
    fetch: ((url, options) => new Promise<Response>(resolve => {
      requests.push({ url: String(url), signal: options!.signal!, resolve });
    })) as typeof fetch,
    later: (fn, ms) => { const timer = { fn, ms }; timers.push(timer); return timer; },
    cancel: timer => { const index = timers.indexOf(timer as typeof timers[0]); if (index >= 0) timers.splice(index, 1); },
    hidden: () => hidden, visibility: fn => { visibility = fn; return () => { subscribed = false; }; },
    createUrl: () => `blob:${++urls}`, revokeUrl: url => { revoked.push(url); }, now: () => 100,
  };
  const stop = startTaskPreview("T", v => views.push(v), env);
  return { requests, timers, revoked, views, stop,
    get view() { return views.at(-1)!; }, get subscribed() { return subscribed; },
    hide(value: boolean) { hidden = value; visibility(); },
    async reply(response: Response) { requests.at(-1)!.resolve(response); await flush(); },
    async next() { assert.equal(timers.length, 1); timers.shift()!.fn(); await flush(); },
  };
}

test("accepts PNG newer than state; late state never moves the cursor backwards", async () => {
  const h = harness();
  await h.reply(state(frame(1)));
  assert.match(h.requests.at(-1)!.url, /after_frame_id=-1/);
  await h.reply(pixels(frame(2)));
  assert.equal(h.view.frame?.frame_id, 2);
  assert.equal(h.timers[0].ms, 500);
  await h.next();
  await h.reply(state(frame(1)));
  assert.equal(h.requests.length, 3); // No duplicate PNG for the late state.
  assert.equal(h.view.frame?.frame_id, 2);
  await h.next();
  await h.reply(state(frame(3)));
  assert.match(h.requests.at(-1)!.url, /after_frame_id=2/);
  await h.reply(pixels(frame(4)));
  assert.deepEqual(h.revoked, ["blob:1"]);
  h.stop();
  assert.deepEqual(h.revoked, ["blob:1", "blob:2"]);
});

test("unavailable/action-pending labels the retained image as historical", async () => {
  const h = harness();
  await h.reply(state()); await h.reply(pixels());
  await h.next();
  await h.reply(Response.json({ task_id: "T", task_status: "paused", preview_state: "waiting",
    reason: "post_action_frame_pending", frame: null }));
  assert.equal(h.view.live, false);
  assert.equal(h.view.imageUrl, "blob:1");
  assert.equal(h.timers[0].ms, 1000);
  h.stop();
});

test("generation/run change drops the old image before requesting new pixels", async () => {
  const h = harness();
  await h.reply(state()); await h.reply(pixels());
  await h.next(); await h.reply(state(frame(1, "run-2", 2)));
  assert.deepEqual(h.revoked, ["blob:1"]);
  assert.match(h.requests.at(-1)!.url, /after_frame_id=-1/);
  await h.reply(pixels(frame(1, "run-1", 1)));
  assert.equal(h.view.imageUrl, null);
  assert.equal(h.view.live, false);
  h.stop();
});

test("failed PNG does not advance cursor; 409 rechecks state; 204 keeps pixels", async () => {
  const h = harness();
  await h.reply(state()); await h.reply(pixels());
  await h.next(); await h.reply(state(frame(2)));
  await h.reply(new Response(null, { status: 503 }));
  assert.equal(h.view.frame?.frame_id, 1);
  await h.next(); await h.reply(state(frame(2)));
  assert.match(h.requests.at(-1)!.url, /after_frame_id=1/);
  await h.reply(new Response(null, { status: 409 }));
  assert.equal(h.timers[0].ms, 500);
  await h.next(); assert.match(h.requests.at(-1)!.url, /\/state$/);
  await h.reply(state(frame(2))); await h.reply(new Response(null, { status: 204 }));
  assert.equal(h.view.imageUrl, "blob:1");
  assert.equal(h.view.frame?.frame_id, 1);
  h.stop();
});

test("errors back off 1/2/5 seconds and 429 respects Retry-After", async () => {
  const h = harness();
  for (const ms of [1000, 2000, 5000, 5000]) {
    await h.reply(new Response(null, { status: 503 }));
    assert.equal(h.timers[0].ms, ms); await h.next();
  }
  await h.reply(new Response(null, { status: 429, headers: { "Retry-After": "3" } }));
  assert.equal(h.timers[0].ms, 3000);
  h.stop();
});

test("one request stays in flight; hide aborts and ignores a late response", async () => {
  const h = harness();
  assert.equal(h.requests.length, 1); assert.equal(h.timers.length, 0);
  const stale = h.requests[0];
  h.hide(true); assert.equal(stale.signal.aborted, true);
  stale.resolve(state()); await flush();
  assert.equal(h.requests.length, 1); assert.equal(h.timers.length, 0);
  h.hide(false); assert.equal(h.requests.length, 2);
  assert.match(h.requests.at(-1)!.url, /\/state$/);
  await h.reply(state()); const pending = h.requests.at(-1)!;
  h.stop(); pending.resolve(pixels()); await flush();
  assert.equal(h.subscribed, false); assert.equal(h.timers.length, 0);
  assert.equal(h.view.imageUrl, null);
});

test("unmount revokes displayed pixels and a resumed old request cannot replace them", async () => {
  const h = harness(); await h.reply(state()); await h.reply(pixels());
  await h.next(); await h.reply(state(frame(2)));
  const stale = h.requests.at(-1)!;
  h.hide(true); h.hide(false);
  stale.resolve(pixels(frame(2))); await flush();
  assert.equal(h.view.frame?.frame_id, 1);
  assert.equal(h.view.live, false);
  h.stop(); assert.deepEqual(h.revoked, ["blob:1"]);
});

test("invalid geometry, task, age, identity or unbounded epoch cannot become pixels", () => {
  for (const bad of [ { ...frame(), task_id: "other" }, { ...frame(), width: 1 },
    { ...frame(), frame_id: -1 }, { ...frame(), frame_age_ms: NaN },
    { ...frame(), run_epoch: "x".repeat(129) } ]) {
    assert.throws(() => parsePreviewFrame(bad, "T"));
  }
});
