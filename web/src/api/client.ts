// Thin fetch wrappers over the Control API. All endpoints are relative to the
// origin (dev: proxied by Vite; prod: served by the same process via
// StaticFiles). Artifact refs are fetched as raw text/JSON for the LLM
// input/output viewers and the semantic-tree renderer.
import type { DeviceInfo, FailedTask, SkillLinks, SkillSummary, Task, TaskTimeline, TraceEvent, TreeRefPayload, ModelCatalog } from "./types";
export const DEFAULT_SKILL_LEARN = false;
async function getJson<T>(path: string): Promise<T> {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok)
        throw new Error(`${res.status} ${await res.text()}`);
    return res.json() as Promise<T>;
}
async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
        method,
        headers: {
            Accept: "application/json",
            ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok)
        throw new Error(`${res.status} ${await res.text()}`);
    return res.json() as Promise<T>;
}
export async function listTasks(): Promise<Task[]> {
    return getJson<Task[]>("/api/tasks?summary=true");
}
export interface TaskPage {
    items: Task[];
    total: number;
    page: number;
    page_size: number;
    indexing?: boolean;
    index_error?: boolean;
}
export async function getTaskPage(page: number, pageSize: 10 | 20, status?: "failed"): Promise<TaskPage> {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (status) query.set("status", status);
    return getJson<TaskPage>(`/api/tasks/page?${query}`);
}
export async function getTask(id: string): Promise<Task> {
    return getJson<Task>(`/api/tasks/${id}`);
}
export interface JevCheckEntry {
    kind: string;
    key: string;
    version: number;
    check_index: number;
    report_ref: string;
    io_ref?: string | null;
    source_steps: number[];
    invocation_ids?: string[];
    committed_summary?: string | null;
    status?: string | null;
    reason?: string | null;
}
export async function getJevChecks(id: string): Promise<{ checks: JevCheckEntry[] }> {
    return getJson(`/api/tasks/${encodeURIComponent(id)}/jev-checks`);
}
export async function getTimeline(id: string): Promise<TaskTimeline> {
    return getJson<TaskTimeline>(`/api/tasks/${id}/timeline`);
}
export async function listFailedTasks(): Promise<FailedTask[]> {
    return getJson<FailedTask[]>("/api/tasks/failed/list");
}
export async function listTraces(id: string, level?: string): Promise<TraceEvent[]> {
    const q = level ? `?level=${encodeURIComponent(level)}` : "";
    return getJson<TraceEvent[]>(`/api/tasks/${id}/traces${q}`);
}
export function createTaskModelOverrides(opts?: {
    decision_model?: string | null;
    executor_model?: string | null;
}): {
    manager_model?: string;
    executor_model?: string;
} {
    const decision = (opts?.decision_model || "").trim();
    const executor = (opts?.executor_model || "").trim();
    const body: {
        manager_model?: string;
        executor_model?: string;
    } = {};
    // Planner / Reviewer share one catalog id; sessions stay independent.
    if (decision)
        body.manager_model = decision;
    if (executor)
        body.executor_model = executor;
    return body;
}
export async function createTask(instruction: string, deviceSerials: string[], opts?: {
    skill_learn?: boolean;
    decision_model?: string | null;
    executor_model?: string | null;
}): Promise<{
    tasks: Task[];
}> {
    const body: Record<string, unknown> = {
        instruction,
        device_serials: deviceSerials,
        skill_learn: opts?.skill_learn ?? DEFAULT_SKILL_LEARN,
        ...createTaskModelOverrides(opts),
    };
    const res = await fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!res.ok)
        throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
}
export async function getModelCatalog(): Promise<ModelCatalog> {
    return getJson<ModelCatalog>("/api/models");
}
export type CancelTaskResponse = {
    task_id: string;
    status: string;
    cancel_requested: boolean;
    already_terminal: boolean;
};
export async function cancelTask(id: string): Promise<CancelTaskResponse> {
    const res = await fetch(`/api/tasks/${id}/cancel`, { method: "POST" });
    if (!res.ok)
        throw new Error(`${res.status} ${await res.text()}`);
    return res.json() as Promise<CancelTaskResponse>;
}
export async function pauseTask(id: string): Promise<{ task_id: string; status: string }> {
    return sendJson(`/api/tasks/${encodeURIComponent(id)}/pause`, "POST");
}
export async function resumeTask(id: string): Promise<{ task_id: string; status: string }> {
    return sendJson(`/api/tasks/${encodeURIComponent(id)}/resume`, "POST");
}
export async function listDevices(): Promise<DeviceInfo[]> {
    return getJson<DeviceInfo[]>("/api/devices");
}
/** Display label: market_name || model || serial */
export function deviceLabel(d: Pick<DeviceInfo, "serial" | "model" | "market_name">): string {
    return d.market_name || d.model || d.serial;
}
/** Build an encoded artifact URL, scoped to a task whenever its id is known. */
export function artifactUrl(ref: string, taskId?: string): string {
    const encodedRef = ref.split("/").map(encodeURIComponent).join("/");
    return taskId
        ? `/api/tasks/${encodeURIComponent(taskId)}/artifacts/${encodedRef}`
        : `/api/artifacts/${encodedRef}`;
}
/** Fetch a raw artifact (LLM input/output) as text. */
export async function getArtifactText(ref: string, taskId?: string): Promise<string> {
    const res = await fetch(artifactUrl(ref, taskId), { headers: { Accept: "text/plain" } });
    if (!res.ok)
        throw new Error(`${res.status}`);
    return res.text();
}
/** Fetch a JSON artifact (semantic-tree payload) and parse it. */
export async function getArtifactJson<T = TreeRefPayload>(ref: string, taskId?: string): Promise<T> {
    const res = await fetch(artifactUrl(ref, taskId), { headers: { Accept: "application/json" } });
    if (!res.ok)
        throw new Error(`${res.status}`);
    return res.json() as Promise<T>;
}
export interface HealthPayload {
    ok: boolean;
    skill_miner_enabled: boolean;
    driver: {
        ok?: boolean;
        error?: string;
        [k: string]: unknown;
    };
    devices?: DeviceInfo[];
    runtime: string;
}
export async function getHealth(): Promise<HealthPayload> {
    return getJson<HealthPayload>("/api/health");
}
export type SkillUpsert = {
    id: string;
    name?: string;
    description?: string;
    version?: string;
    app?: string | null;
    kind?: "generic" | "app_core" | "workflow" | "candidate";
    capability?: string;
    tags?: string[];
    triggers?: string[];
    body?: string;
};
export type SkillUpdate = {
    app?: string | null;
    kind?: "generic" | "app_core" | "workflow" | "candidate" | null;
    capability?: string | null;
    tags?: string[] | null;
    triggers?: string[] | null;
    description?: string | null;
    version?: string | null;
    body?: string | null;
};
export async function listSkills(params?: {
    app?: string;
    q?: string;
}): Promise<SkillSummary[]> {
    const sp = new URLSearchParams();
    if (params?.app)
        sp.set("app", params.app);
    if (params?.q)
        sp.set("q", params.q);
    const q = sp.toString();
    return getJson<SkillSummary[]>(`/api/skills${q ? `?${q}` : ""}`);
}
export async function getSkill(id: string): Promise<SkillSummary> {
    return getJson<SkillSummary>(`/api/skills/${encodeURIComponent(id)}`);
}
export async function createSkill(body: SkillUpsert): Promise<SkillSummary> {
    return sendJson<SkillSummary>("/api/skills", "POST", body);
}
export async function updateSkill(id: string, body: SkillUpdate): Promise<SkillSummary> {
    return sendJson<SkillSummary>(`/api/skills/${encodeURIComponent(id)}`, "PUT", body);
}
export async function deleteSkill(id: string): Promise<{
    ok: boolean;
}> {
    return sendJson<{
        ok: boolean;
    }>(`/api/skills/${encodeURIComponent(id)}`, "DELETE");
}
export async function getTaskSkillLinks(taskId: string): Promise<SkillLinks> {
    return getJson<SkillLinks>(`/api/tasks/${encodeURIComponent(taskId)}/skill-links`);
}
export async function listPendingSkills(): Promise<import("./types").PendingSkill[]> {
    return getJson("/api/skills/pending");
}
export async function getPendingSkill(id: string): Promise<{
    id: string;
    gist: string;
    target: string;
    diff: string;
    new_text: string;
    old_text: string;
    meta?: Record<string, unknown>;
}> {
    return getJson(`/api/skills/pending/${encodeURIComponent(id)}`);
}
export async function approvePendingSkill(id: string): Promise<{
    ok: boolean;
}> {
    return sendJson(`/api/skills/pending/${encodeURIComponent(id)}/approve`, "POST", {});
}
export async function rejectPendingSkill(id: string, reason = ""): Promise<{
    ok: boolean;
}> {
    return sendJson(`/api/skills/pending/${encodeURIComponent(id)}/reject`, "POST", {
        reason,
    });
}
export type PersonalLearningRequest = {
    accept_model_cost: boolean;
    allow_device_operations: boolean;
    max_calls: number;
    max_actions: number;
    max_seconds: number;
};
export type LearningPreferences = {
    local_recording: boolean;
    contribution_enabled: boolean;
};
export function getLearningPreferences(): Promise<LearningPreferences> {
    return getJson("/api/learning/preferences");
}
export function getLearningDefaults(): Promise<{ max_calls: number; max_actions: number; max_seconds: number }> {
    return getJson("/api/learning/defaults");
}
export function updateLearningPreferences(value: LearningPreferences): Promise<LearningPreferences> {
    return sendJson("/api/learning/preferences", "PUT", value);
}
export function getLearningContribution(taskId: string): Promise<Record<string, unknown>> {
    return getJson(`/api/tasks/${encodeURIComponent(taskId)}/learning-contribution`);
}
export async function learnFromTask(taskId: string, request: PersonalLearningRequest): Promise<{
    ok: boolean;
    skipped?: boolean;
    pending_id?: string;
    gist?: string;
    error?: string;
    reason?: string;
}> {
    return sendJson(`/api/tasks/${encodeURIComponent(taskId)}/learn`, "POST", request);
}

export interface LearningTraceEntry {
    ref: string;
    order: number;
    role: "learner" | "reviewer";
    phase: string;
    status: string;
    created_at: number;
    model?: string | null;
}
export interface LearningTraceSession {
    job_id: string;
    source_task_id: string;
    origin: string;
    catalog_ref?: string | null;
    latest_phase?: string | null;
    entry_total: number;
    older_entry_count: number;
    omitted_entry_count: number;
    cost: Record<string, unknown> | null;
    updated_at: number;
    entries: LearningTraceEntry[];
    histories: Array<{ namespace: string; task_id: string; available: boolean; status: string | null; data_source: string | null }>;
}
export interface LearningTraces {
    task_id: string;
    running: boolean;
    session_total: number;
    session_offset: number;
    session_limit: number;
    result_total: number;
    result_offset: number;
    result_limit: number;
    pending_omitted_count: number;
    sessions: LearningTraceSession[];
    pending: Array<{ id: string; status: string; gist: string; target: string; verdict: string | null; review: Record<string, unknown> }>;
    results: Array<{ ref: string; status: string; reason: string | null; created_at: number; cost: Record<string, unknown> | null }>;
    notes: string[];
    policy: string;
}
export async function getTaskLearningTraces(id: string, options: {
    session_offset?: number; result_offset?: number; job_id?: string; entry_before?: number;
} = {}): Promise<LearningTraces> {
    const query = new URLSearchParams(Object.entries(options).map(([key, value]) => [key, String(value)]));
    return getJson(`/api/tasks/${encodeURIComponent(id)}/learning-traces?${query}`);
}
