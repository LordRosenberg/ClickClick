import assert from "node:assert/strict";
import test from "node:test";

import {
  createTaskModelOverrides,
  DEFAULT_SKILL_LEARN,
} from "../src/api/client.ts";

test("new tasks do not enable SkillLearner unless explicitly requested", () => {
  assert.equal(DEFAULT_SKILL_LEARN, false);
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
