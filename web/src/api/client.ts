// Thin fetch wrappers over the Control API. All endpoints are relative to the
// origin (dev: proxied by Vite; prod: served by the same process via
// StaticFiles). Artifact refs are fetched as raw text/JSON for the LLM
// input/output viewers and the semantic-tree renderer.
import type { DeviceInfo, FailedTask, SkillLinks, SkillSummary, Task, TaskTimeline, TraceEvent, TreeRefPayload, ModelCatalog, ChatGPTStatus, ChatGPTLoginStart, ChatGPTLoginPoll, } from "./types";
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
export async function getChatGPTStatus(): Promise<ChatGPTStatus> {
    return getJson<ChatGPTStatus>("/api/chatgpt/status");
}
export async function startChatGPTLogin(): Promise<ChatGPTLoginStart> {
    return sendJson<ChatGPTLoginStart>("/api/chatgpt/login/start", "POST");
}
export async function pollChatGPTLogin(): Promise<ChatGPTLoginPoll> {
    return sendJson<ChatGPTLoginPoll>("/api/chatgpt/login/poll", "POST");
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
export async function listDevices(): Promise<DeviceInfo[]> {
    return getJson<DeviceInfo[]>("/api/devices");
}
/** Display label: market_name || model || serial */
export function deviceLabel(d: Pick<DeviceInfo, "serial" | "model" | "market_name">): string {
    return d.market_name || d.model || d.serial;
}
/** ADB serial from a task binding key (strips ``driver_id/`` for remote hubs). */
export function adbSerialFromKey(key: string | null | undefined): string {
    if (!key)
        return "";
    if (key === "fixture")
        return key;
    const i = key.indexOf("/");
    return i >= 0 ? key.slice(i + 1) : key;
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
export async function getScrcpyHint(): Promise<{
    tool: string;
    command: string;
    note: string;
    /** True when local jar and/or remote hubs can serve Live. */
    available: boolean;
    server_jar?: boolean;
    remote_hubs?: string[];
}> {
    return getJson("/api/device/scrcpy");
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
export async function learnFromTask(taskId: string): Promise<{
    ok: boolean;
    skipped?: boolean;
    pending_id?: string;
    gist?: string;
    error?: string;
}> {
    return sendJson(`/api/tasks/${encodeURIComponent(taskId)}/learn`, "POST", {});
}
