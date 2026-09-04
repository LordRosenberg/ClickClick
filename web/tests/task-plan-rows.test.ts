import assert from "node:assert/strict";
import test from "node:test";

import { buildPlanRows } from "../src/lib/taskPlanRows.ts";

test("only effective Reviewer-accepted progress is shown as done", () => {
  const rows = buildPlanRows(
    [
      {
        progress_id: "p1", statement: "输入前半段", effective: false,
        evidence_handles: ["e1"], packet_digest: "d1", accepted_step: 1,
        source_subgoal: "输入内容", superseded_by_packet_digest: "d2",
      },
      {
        progress_id: "p2", statement: "确认完成", effective: true,
        evidence_handles: ["e2"], packet_digest: "d2", accepted_step: 2,
        source_subgoal: "确认结果",
      },
    ],
    ["补最后一位"],
    "补最后一位",
  );

  assert.deepEqual(rows, [
    { text: "确认完成", kind: "done", current: false },
    { text: "补最后一位", kind: "todo", current: true },
  ]);
});
