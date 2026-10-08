import assert from "node:assert/strict";
import test from "node:test";
import { taskCanPause, taskCanResume, taskIsActive, taskIsNonterminal, taskStatusLabel } from "../src/lib/taskLifecycle.ts";

test("paused tasks remain cancellable and resume only from a clean paused state", () => {
  assert.equal(taskIsNonterminal("paused"), true);
  assert.equal(taskIsActive("paused"), false);
  assert.equal(taskIsActive("pausing"), true);
  assert.equal(taskCanResume("pausing"), false);
  assert.equal(taskCanResume("paused"), true);
  assert.equal(taskCanPause("running"), true);
  assert.equal(taskCanPause("paused"), false);
  for (const status of ["failed", "cancelled", "succeeded"]) {
    assert.equal(taskIsNonterminal(status), false);
    assert.equal(taskCanResume(status), false);
  }
  assert.equal(taskCanPause("running", true), false);
  assert.equal(taskCanResume("paused", true), false);
  assert.equal(taskStatusLabel.pausing, "正在暂停");
  assert.equal(taskStatusLabel.paused, "已暂停");
});
