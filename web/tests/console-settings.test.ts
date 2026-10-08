import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyProfile, renameModel, settingsPayload, modelPresets, roleModelOptions, resolvedRoleModel, selectRoleModel } from "../src/lib/console-settings.ts";

test("subscription starts with an editable model and matching default role", () => {
  const profile = emptyProfile("subscription");
  assert.ok(profile.default_model.startsWith("chatgpt/"));
  assert.equal(profile.default_model, profile.models[0].id);
  assert.equal(profile.models[0].stream, true);
  assert.equal(profile.models[0].api_key, "");
});

test("renaming model preserves role references and inherited defaults", () => {
  let profile = emptyProfile();
  profile = renameModel(profile, 0, "openai/model");
  assert.equal(profile.default_model, "openai/model");
  assert.equal(profile.manager_model, "");
  profile.executor_model = "openai/model";
  profile = renameModel(profile, 0, "openai/new");
  assert.equal(profile.default_model, "openai/new");
  assert.equal(profile.executor_model, "openai/new");
});

test("roles offer presets before configuration and create a model only when selected", () => {
  let profile = emptyProfile("subscription");
  const target = "chatgpt/gpt-6-luna";
  assert.ok(roleModelOptions("subscription", profile).some(model => model.id === target));
  profile = selectRoleModel(profile, "executor_model", target, "subscription");
  assert.equal(profile.executor_model, target);
  assert.equal(profile.models.length, 2);
  assert.equal(profile.models[1].reasoning_effort, "high");
  profile = selectRoleModel(profile, "manager_model", target, "subscription");
  assert.equal(profile.models.length, 2);
  assert.equal(profile.default_model, "chatgpt/gpt-5.6-sol");
  assert.equal(selectRoleModel(profile, "executor_model", "", "subscription").executor_model, "");
  assert.ok(modelPresets("api").every(model => model.id.startsWith("openai/")));
});

test("API preset selection and rename retain saved credential references without exposing keys", () => {
  const profile = emptyProfile("api");
  profile.models[0].api_key_configured = true;
  profile.models[0].base_url = "https://relay.example/v1";
  const selected = selectRoleModel(profile, "executor_model", "openai/gpt-6-luna", "api");
  assert.equal(selected.models[1].credentials_from, profile.models[0].id);
  assert.equal(selected.models[1].api_key, "");
  assert.equal(selected.models[1].base_url, "https://relay.example/v1");
  const renamed = renameModel(selected, 0, "openai/custom");
  assert.equal(renamed.models[0].credentials_from, profile.models[0].id);
  assert.ok(roleModelOptions("api", renamed).some(model => model.id === "openai/custom"));
});

test("role parameter editor follows runtime model inheritance rather than unrelated defaults", () => {
  let profile = emptyProfile("subscription");
  profile = selectRoleModel(profile, "manager_model", "chatgpt/gpt-6-astra", "subscription");
  assert.equal(resolvedRoleModel(profile, "executor_model"), "chatgpt/gpt-5.6-sol");
  assert.equal(resolvedRoleModel(profile, "skill_learner_model"), "chatgpt/gpt-6-astra");
  assert.equal(resolvedRoleModel(profile, "skill_reviewer_model"), "chatgpt/gpt-6-astra");
  profile = selectRoleModel(profile, "skill_learner_model", "chatgpt/gpt-6-luna", "subscription");
  assert.equal(resolvedRoleModel(profile, "skill_reviewer_model"), "chatgpt/gpt-6-luna");
});

test("saving strips summary metadata but preserves blank-key and advanced-body semantics", () => {
  const profile = renameModel(emptyProfile(), 0, "openai/model");
  profile.models[0].api_key_configured = true;
  profile.models[0].extra_body_configured = true;
  const runtime = { agent_architecture: "plan_executor" as const, executor_context_tokens: 16000,
    chatgpt_history_tokens: null, default_task_model_calls: 20, default_task_device_actions: null,
    default_task_seconds: 600, learning_default_calls: 24, learning_default_actions: 30,
    learning_default_seconds: 900, restart_required: false, credentials_validated: false };
  const payload = settingsPayload("api", profile, runtime);
  assert.equal(payload.models[0].api_key, "");
  assert.equal(payload.models[0].extra_body, null);
  assert.ok(!("api_key_configured" in payload.models[0]));
  assert.ok(!("restart_required" in payload));
  assert.ok(!("credentials_validated" in payload));
  profile.models[0].extra_body = "{}";
  assert.equal(settingsPayload("api", profile, runtime).models[0].extra_body, "{}");
});
