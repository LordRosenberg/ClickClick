import assert from "node:assert/strict";
import test from "node:test";

import {
  createTask,
  createTaskModelOverrides,
  DEFAULT_SKILL_LEARN,
} from "../src/api/client.ts";

test("new tasks do not enable SkillLearner unless explicitly requested", () => {
  assert.equal(DEFAULT_SKILL_LEARN, false);
});

test("only an explicitly enabled task requests script recording", async (context) => {
  const requests: Array<Record<string, unknown>> = [];
  context.mock.method(globalThis, "fetch", async (_url: string, options: { body: string }) => {
    requests.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ tasks: [] }));
  });

  await createTask("open settings", ["fixture"]);
  await createTask("open settings", ["fixture"], { generate_test_script: false });
  await createTask("open settings", ["fixture"], { generate_test_script: true });

  assert.equal("generate_test_script" in requests[0], false);
  assert.deepEqual(requests[1], requests[0]);
  assert.deepEqual(requests[2], { ...requests[0], generate_test_script: true });
});

test("decision model is shared by planner/reviewer", () => {
  assert.deepEqual(
    createTaskModelOverrides({
      decision_model: "chatgpt/gpt-5.4",
      executor_model: "openai/MiniMax-M3",
    }),
    {
      manager_model: "chatgpt/gpt-5.4",
      executor_model: "openai/MiniMax-M3",
    },
  );
});
