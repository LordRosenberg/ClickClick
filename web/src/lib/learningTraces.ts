import { t } from "./locale.ts";
// Rendering projections only; model statements never become execution outcomes.
export function object(value: unknown): Record<string, unknown> {
    return value != null && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown> : {};
}
export function pretty(value: unknown): string {
    if (typeof value === "string") {
        try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
    }
    return JSON.stringify(value ?? null, null, 2);
}
export function tracePayload(value: unknown): Record<string, unknown> {
    const record = object(value);
    return record.payload ? object(record.payload) : record;
}
export function transcriptMessages(value: unknown): Record<string, unknown>[] {
    const payload = tracePayload(value);
    const messages = Array.isArray(payload.messages) ? payload.messages
        : [...(Array.isArray(payload.phase_input) ? payload.phase_input : []),
           ...(Array.isArray(payload.history) ? payload.history : [])];
    return messages.map(object);
}
export function learningCost(cost: unknown): string {
    const data = object(cost);
    const count = (key: string) => typeof data[key] === "number" ? String(data[key]) : "unknown";
    const usage = object(data.provider_usage);
    const totals = (Array.isArray(usage.records) ? usage.records : []).map(object).map(row => row.total_tokens);
    const allKnown = totals.length > 0 && totals.every(value => typeof value === "number") && usage.unknown_responses === 0;
    const tokenText = typeof usage.total_tokens === "number" && usage.unknown_responses === 0
        ? String(usage.total_tokens)
        : allKnown ? String(totals.reduce<number>((sum, value) => sum + Number(value), 0)) : "unknown";
    const reserved = data.calls_include_reserved_upper_bounds || data.actions_include_reserved_upper_bounds ? t(" · 含预留上界") : "";
    return t("{value0} 次请求 · {value1} 次动作 · {value2} s · tokens {value3}{value4}", { value0: count("calls"), value1: count("actions"), value2: count("seconds"), value3: tokenText, value4: reserved });
}
