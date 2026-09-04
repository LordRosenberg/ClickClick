import assert from "node:assert/strict";
import test from "node:test";

import {
  observationFromTracePayload,
  observationRoleWins,
  projectActionToFrame,
  reconcileObservation,
} from "../src/lib/observationReplay.ts";

test("projects dispatched device coordinates through recorded frame geometry", () => {
  const projected = projectActionToFrame(
    { type: "tap_xy", x: 600, y: 1335 },
    [1200, 2670],
    { width: 400, height: 890 },
  );
  assert.equal(projected?.x, 200);
  assert.equal(projected?.y, 445);
});

test("does not fabricate a precise overlay when geometry is missing", () => {
  assert.equal(
    projectActionToFrame(
      { type: "tap_xy", x: 200, y: 300 },
      undefined,
      { width: 400, height: 890 },
    ),
    null,
  );
});

test("live role projection preserves the highest-priority replay envelope", () => {
  const payload = {
    som_ref: "som/executor.png",
    tree_ref: "trees/executor.json",
    observation_mode: "tree_plus_image",
    gap_reasons: ["visual_required"],
    estimated_tokens: 20,
    observation_id: "obs_executor",
    captured_monotonic_ms: 20,
    frame_geometry: [1200, 2670],
  };
  const projected = reconcileObservation(
    observationFromTracePayload({
      ...payload,
      observation_id: "obs_planner",
      frame_geometry: [100, 200],
    }),
    observationFromTracePayload(payload),
    observationRoleWins("planner", "executor"),
  );
  assert.deepEqual(projected, payload);
  assert.equal(observationRoleWins("executor", "reviewer"), false);
  assert.equal(observationRoleWins("planner", "reviewer"), true);
});
