import assert from "node:assert/strict";
import test from "node:test";
import { resolveObservationImage } from "../src/lib/observationImage.ts";
import type { RoleCall } from "../src/api/types.ts";

const shot = { call_key: "executor-1", role: "executor", step_seq: 1,
  observation: { som_ref: "som/old.png", model_image_ref: "som/model.png", frame_geometry: [100, 200] },
  executor: { action: { type: "tap", x: 10, y: 20 } } } as unknown as RoleCall;
const text = { call_key: "planner-2", role: "planner", step_seq: 2 } as RoleCall;

test("follow keeps the latest saved image when a new text-only call arrives", () => {
  const image = resolveObservationImage([shot, text], text, true);
  assert.equal(image?.artifactRef, "som/model.png");
  assert.equal(image?.call, shot);
  assert.equal(image?.action, shot.executor?.action);
  assert.deepEqual(image?.frameGeometry, [100, 200]);
});

test("manual history never borrows another call's image or action", () => {
  assert.equal(resolveObservationImage([shot, text], text, false), null);
  assert.equal(resolveObservationImage([shot, text], shot, false)?.call, shot);
  assert.equal(resolveObservationImage([], null, true), null);
});

test("explicit model image wins and never receives an observation action overlay", () => {
  const image = resolveObservationImage([shot, text], shot, false, { artifactRef: "llm/selected.png", rowKey: "row-1" });
  assert.equal(image?.artifactRef, "llm/selected.png");
  assert.equal(image?.selectionKey, "row-1");
  assert.equal(image?.source, "model");
  assert.equal(image?.action, null);
  assert.equal(image?.frameGeometry, undefined);
});

test("a planner image keeps its own geometry and no executor action", () => {
  const planner = { ...text, observation: { som_ref: "som/planner.png", frame_geometry: [200, 100] } } as RoleCall;
  const image = resolveObservationImage([shot, planner], planner, true);
  assert.equal(image?.artifactRef, "som/planner.png");
  assert.equal(image?.action, null);
  assert.deepEqual(image?.frameGeometry, [200, 100]);
});
