import assert from "node:assert/strict";
import test from "node:test";
import type { TraceEvent } from "../src/api/types.ts";
import { mergeWorkbenchEvents, suggestedScreenWidth, type FeedRow } from "../src/lib/workbenchFeed.ts";

function event(kind: TraceEvent["kind"], ts: number, payload: TraceEvent["payload"]): TraceEvent {
  return { task_id: "task", kind, level: "INFO", message: "", ts, payload };
}

test("workbench replays durable calls without reopening finished tools or thinking", () => {
  const round = { round_id: "inv:round:1", role: "planner" };
  const tool = { call_id: "call", invocation_id: "inv", name: "submit_planner_decision" };
  const start = event("agent_llm_round_started", 1, round);
  const stream = event("agent_llm_stream", 2, { ...round, attempt: 1, sequence: 1, progress_text: "Checking" });
  const latest = event("agent_llm_stream", 3, { ...round, attempt: 1, sequence: 2, progress_text: "Checking dates" });
  const finish = event("agent_llm_round_finished", 4, { ...round, progress_text: "Checked dates" });
  const started = event("agent_tool_started", 5, tool);
  const finished = event("agent_tool_finished", 6, { ...tool, status: "succeeded" });
  let rows = mergeWorkbenchEvents(new Map<string, FeedRow>(), [start, latest, stream]);
  const activeRound = rows.get("round:inv:round:1");
  assert.equal(activeRound?.kind, "thinking");
  assert.equal(activeRound.text, "Checking dates");
  rows = mergeWorkbenchEvents(rows, [finish, finished, start, stream, started]);
  assert.equal(rows.size, 2);
  const finishedRound = rows.get("round:inv:round:1");
  const finishedTool = rows.get("tool:inv:call");
  assert.equal(finishedRound?.kind, "thinking");
  assert.equal(finishedRound.text, "Checked dates");
  assert.equal(finishedTool?.kind, "tool");
  assert.equal(finishedTool.ts, 5);
  assert.equal(finishedTool.status, "succeeded");
});

test("preview width follows image aspect ratio and preserves space for the composer", () => {
  assert.equal(suggestedScreenWidth(1440, 9 / 20, false), 360);
  assert.equal(Math.round(suggestedScreenWidth(1440, 16 / 9, false)), 806);
  assert.equal(suggestedScreenWidth(1000, 16 / 9, false), 552);
  assert.equal(suggestedScreenWidth(1000, 16 / 9, true), 642);
});
