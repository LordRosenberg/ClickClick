import assert from "node:assert/strict";
import test from "node:test";
import { cancelTaskSkillLoop, getTaskSkillLoop, learnFromTask } from "../src/api/client.ts";
import { skillLoopApiError, skillLoopIsActive, skillLoopStatusLabel } from "../src/lib/skillLoop.ts";

test("skill regression starts, polls and stops on the source task's learning routes", async context => {
  const requests: Array<{ url: string; method?: string; body?: BodyInit | null }> = [];
  const closure = { status: "queued" };
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    requests.push({ url, method: options.method, body: options.body });
    return Response.json({ ok: true, closure });
  });

  const consent = { accept_model_cost: true, allow_device_operations: true, max_calls: 24,
    max_actions: 30, max_seconds: 900, run_regression: true };
  assert.deepEqual((await learnFromTask("source/task", consent)).closure, closure);
  assert.deepEqual((await getTaskSkillLoop("source/task")).closure, closure);
  await cancelTaskSkillLoop("source/task");

  assert.deepEqual(requests.map(({ url, method, body }) => [url, method, body]), [
    ["/api/tasks/source%2Ftask/learn", "POST", JSON.stringify(consent)],
    ["/api/tasks/source%2Ftask/skill-loop", undefined, undefined],
    ["/api/tasks/source%2Ftask/skill-loop/cancel", "POST", undefined],
  ]);
});

test("status keeps an active paired run in progress and presents its terminal review result", () => {
  assert.equal(skillLoopIsActive({ status: "queued" }), true);
  assert.equal(skillLoopIsActive({ status: "running", phase: "cancelling" }), true);
  assert.equal(skillLoopStatusLabel({ status: "running", phase: "regression" }), "正在比较原 Skill 与候选 Skill…");
  assert.equal(skillLoopStatusLabel({ status: "awaiting_publication_review", phase: "completed" }), "配对回归通过，候选已进入待审批。");
  assert.equal(skillLoopIsActive({ status: "awaiting_publication_review" }), false);
  assert.equal(skillLoopIsActive(null), false);
});

test("a missing test emulator is explained from the start request error", () => {
  const error = new Error('409 {"detail":"fixture_not_configured"}');
  assert.equal(skillLoopApiError(error), "请先配置 Skills 回归专用 Android 模拟器和干净快照。");
});
