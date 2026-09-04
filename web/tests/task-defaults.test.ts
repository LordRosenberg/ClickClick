import assert from "node:assert/strict";
import test from "node:test";

import { DEFAULT_SKILL_LEARN } from "../src/api/client.ts";

test("new tasks do not enable SkillLearner unless explicitly requested", () => {
  assert.equal(DEFAULT_SKILL_LEARN, false);
});
