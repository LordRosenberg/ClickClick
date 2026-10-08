export type SetupRequest = <T>(path: string, body?: unknown) => Promise<T>;
export type Mode = "api" | "subscription";
// Shortcut names, not an account entitlement list. Keep custom IDs available.
// Source: https://developers.openai.com/api/docs/models/all (2026-10-07).
const GPT_PRESETS = [
  ["gpt-5.6-sol", "GPT-5.6 Sol"],
  ["gpt-6.1-sol", "GPT-6.1 Sol"],
  ["gpt-6-astra", "GPT-6 Astra"],
  ["gpt-6-sol", "GPT-6 Sol"],
  ["gpt-6-luna", "GPT-6 Luna"],
  ["gpt-5.6-terra", "GPT-5.6 Terra"],
  ["gpt-5.6-luna", "GPT-5.6 Luna"],
] as const;
export function modelPresets(mode: Mode) {
  return GPT_PRESETS.map(([slug, label]) => ({ id: `${mode === "api" ? "openai" : "chatgpt"}/${slug}`, label }));
}
export const ROLE_LABELS = {
  default_model: "默认模型", manager_model: "Planner + Reviewer", executor_model: "Executor",
  skill_learner_model: "Skill Learner", skill_reviewer_model: "Skill Reviewer",
} as const;
export type Role = keyof typeof ROLE_LABELS;
export type ModelSettings = {
  id: string; base_url: string; api_key?: string; api_key_configured?: boolean; user_agent: string;
  stream: boolean; reasoning_supported: boolean; reasoning_effort: string; reasoning_summary: string;
  max_tokens: number | null; history_tokens: number | null; max_input_tokens: number | null;
  tool_choice: "required" | "auto"; allowed_openai_params: string[];
  extra_body: string | null; extra_body_configured?: boolean;
  credentials_from?: string;
};
export type Profile = Record<Role, string> & { models: ModelSettings[] };
export type RuntimeSettings = {
  agent_architecture: "plan_executor" | "plan_reviewer";
  executor_context_tokens: number; chatgpt_history_tokens: number | null;
  default_task_model_calls: number | null; default_task_device_actions: number | null; default_task_seconds: number | null;
  learning_default_calls: number; learning_default_actions: number; learning_default_seconds: number;
};
export type JevSettings = {
  mode: "off" | "shadow" | "enforce"; base_url: string; model: string;
  api_key?: string; api_key_configured?: boolean; timeout_s: number; failure_policy: "bypass" | "rollback";
};
export type SettingsSummary = RuntimeSettings & { mode: Mode; profiles: Partial<Record<Mode, Profile>>; jev?: JevSettings };
const RUNTIME_KEYS: Array<keyof RuntimeSettings> = ["agent_architecture", "executor_context_tokens", "chatgpt_history_tokens",
  "default_task_model_calls", "default_task_device_actions", "default_task_seconds",
  "learning_default_calls", "learning_default_actions", "learning_default_seconds"];

export function emptyModel(): ModelSettings {
  return { id: "", base_url: "", api_key: "", user_agent: "", stream: false, reasoning_supported: false,
    reasoning_effort: "", reasoning_summary: "", max_tokens: null, history_tokens: null, max_input_tokens: null,
    tool_choice: "required", allowed_openai_params: [], extra_body: null };
}
export function emptyProfile(mode: Mode = "api"): Profile {
  const model = emptyModel();
  model.id = "openai/gpt-5.6-sol";
  if (mode === "subscription") {
    // Same initial subscription example as the deployment guide; editable for
    // the account's actual model availability, never an entitlement claim.
    model.id = "chatgpt/gpt-5.6-sol"; model.stream = true;
    model.reasoning_supported = true; model.reasoning_effort = "high"; model.reasoning_summary = "concise";
  }
  return { models: [model], default_model: model.id, manager_model: "", executor_model: "",
    skill_learner_model: "", skill_reviewer_model: "" };
}
export function renameModel(profile: Profile, index: number, id: string): Profile {
  const model = profile.models[index];
  const old = model.id;
  const credentials_from = model.credentials_from || (model.api_key_configured ? old : undefined);
  const result = { ...profile, models: profile.models.map((model, i) => i === index ? { ...model, id, credentials_from } : model) };
  for (const role of Object.keys(ROLE_LABELS) as Role[]) {
    if (result[role] === old && (old !== "" || role === "default_model")) result[role] = id;
  }
  return result;
}
export function roleModelOptions(mode: Mode, profile: Profile) {
  const presets = modelPresets(mode);
  const ids = new Set(presets.map(model => model.id));
  return [...presets, ...profile.models.filter(model => model.id && !ids.has(model.id))
    .map(model => ({ id: model.id, label: model.id }))];
}
export function resolvedRoleModel(profile: Profile, role: Role): string {
  if (role === "skill_reviewer_model") return profile.skill_reviewer_model || resolvedRoleModel(profile, "skill_learner_model");
  if (role === "skill_learner_model") return profile.skill_learner_model || resolvedRoleModel(profile, "manager_model");
  return profile[role] || profile.default_model;
}
export function selectRoleModel(profile: Profile, role: Role, id: string, mode: Mode): Profile {
  if (!id || profile.models.some(model => model.id === id)) return { ...profile, [role]: id };
  if (!modelPresets(mode).some(model => model.id === id)) return profile;
  const source = profile.models.find(model => model.id === profile.default_model) ?? profile.models[0];
  const model = { ...(source ?? emptyModel()), id };
  if (mode === "api") model.credentials_from = source?.credentials_from || (source?.api_key_configured ? source.id : undefined);
  return { ...profile, [role]: id, models: [...profile.models, model] };
}
export function settingsPayload(mode: Mode, profile: Profile, runtime: RuntimeSettings, jev?: JevSettings) {
  const values = Object.fromEntries(RUNTIME_KEYS.map(key => [key, runtime[key]]));
  const jevValues = jev ? { mode: jev.mode, base_url: jev.base_url, model: jev.model,
    api_key: jev.api_key ?? "", timeout_s: jev.timeout_s, failure_policy: jev.failure_policy } : undefined;
  return { ...values, ...profile, mode, ...(jevValues ? { jev: jevValues } : {}), models: profile.models.map(({ api_key_configured: _key,
    extra_body_configured: _extra, ...model }) => ({ ...model, api_key: model.api_key ?? "" })) };
}
