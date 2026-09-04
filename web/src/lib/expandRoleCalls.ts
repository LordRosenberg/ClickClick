import type {
  AgentLlmRound,
  AgentToolCall,
  RoleCall,
  RoleCallRole,
  Step,
  TraceEvent,
  UsageMetrics,
} from "@/api/types";
import { upsertLlmRound, upsertToolCall } from "./agentCalls.ts";

function phaseFor(call: Pick<RoleCall, "role" | "reviewer">): RoleCall["phase"] {
  if (call.role === "reviewer") {
    return call.reviewer?.kind === "task_scope" ? "scope" : "boundary";
  }
  return call.role === "planner" ? "planning" : "execution";
}

export function usageMetrics(rounds: AgentLlmRound[] = []): UsageMetrics {
  const inputs = rounds.map((round) => round.usage?.input_tokens).filter((v): v is number => v != null);
  const outputs = rounds.map((round) => round.usage?.output_tokens).filter((v): v is number => v != null);
  const cached = rounds.map((round) => round.usage?.cached_read_tokens).filter((v): v is number => v != null);
  const inputTotal = inputs.length > 0 ? inputs.reduce((sum, value) => sum + value, 0) : null;
  const cacheTotal = inputs.length > 0 && cached.length === inputs.length
    ? cached.reduce((sum, value) => sum + value, 0)
    : null;
  return {
    round_count: rounds.length,
    input_tokens_total: inputTotal,
    output_tokens_total: outputs.length > 0 ? outputs.reduce((sum, value) => sum + value, 0) : null,
    llm_latency_ms_total: rounds.length > 0
      ? rounds.reduce((sum, round) => sum + (round.latency_ms ?? 0), 0)
      : null,
    cache_read_tokens_total: cacheTotal,
    cache_read_ratio: inputTotal && cacheTotal != null ? cacheTotal / inputTotal : null,
  };
}

/** Stable composite key matching control_api.services.call_key_for. */
export function callKeyFor(
  stepSeq: number | null | undefined,
  role: RoleCallRole,
  ordinal = 1,
): string {
  const seqPart = stepSeq == null ? "null" : String(stepSeq);
  return `${seqPart}:${role}:${ordinal}`;
}

function tickForRole(step: Step, role: RoleCallRole) {
  return role === "reviewer"
    ? step.reviewer
    : role === "planner"
      ? step.planner
      : step.executor;
}

function tickForCall(call: RoleCall) {
  return call.role === "reviewer"
    ? call.reviewer
    : call.role === "planner"
      ? call.planner
      : call.executor;
}

function ownsInvocation(call: RoleCall, invocationId: string): boolean {
  const tick = tickForCall(call);
  return [
    ...(tick?.agent_rounds ?? []),
    ...(tick?.tool_calls ?? []),
  ].some((item) => item.invocation_id === invocationId);
}

/** Append one exact SSE role call, or replace its replayed invocation in place. */
export function upsertRoleCall(
  current: RoleCall[],
  step: Step,
  role: RoleCallRole,
  observation: RoleCall["observation"],
): RoleCall[] {
  const tick = tickForRole(step, role);
  if (!tick) return current;
  const invocationIds = new Set(
    [
      ...(tick.agent_rounds ?? []).map((round) => round.invocation_id),
      ...(tick.tool_calls ?? []).map((call) => call.invocation_id),
    ],
  );
  const replayIndex = invocationIds.size === 0
    ? -1
    : current.findIndex((call) =>
        call.role === role &&
        [
          ...roundsForCall(call).map((round) => round.invocation_id),
          ...((
            call.role === "reviewer"
              ? call.reviewer?.tool_calls
              : call.role === "planner"
                ? call.planner?.tool_calls
                : call.executor?.tool_calls
          ) ?? []).map((tool) => tool.invocation_id),
        ].some((id) => invocationIds.has(id))
      );
  const ordinal = current.filter((call) =>
    call.role === role && (call.step_seq ?? null) === (step.step_seq ?? null)
  ).length + 1;
  const metrics = usageMetrics(tick.agent_rounds);
  const next: RoleCall = {
    call_key: replayIndex >= 0
      ? current[replayIndex].call_key
      : callKeyFor(step.step_seq, role, ordinal),
    step_seq: step.step_seq ?? null,
    role,
    phase: phaseFor({ role, reviewer: role === "reviewer" ? step.reviewer : null }),
    observation,
    reviewer: role === "reviewer" ? step.reviewer : null,
    planner: role === "planner" ? step.planner : null,
    executor: role === "executor" ? step.executor : null,
    elapsed_ms: tick.role_elapsed_ms ?? null,
    llm_elapsed_ms: metrics.llm_latency_ms_total,
    metrics,
    wall_started_at: step.wall_started_at ?? null,
  };
  if (replayIndex < 0) return [...current, next];
  const calls = [...current];
  calls[replayIndex] = next;
  return calls;
}

/** Project one in-flight round/tool event without mutating folded action steps. */
export function upsertLiveRoleEvent(
  current: RoleCall[],
  event: TraceEvent,
): RoleCall[] {
  const payload = event.payload as unknown as AgentLlmRound & AgentToolCall;
  const role = payload.role;
  if (role !== "reviewer" && role !== "planner" && role !== "executor") return current;
  const invocationId = String(payload.invocation_id ?? "");
  if (!invocationId) return current;
  const existing = current.find((call) =>
    call.role === role && ownsInvocation(call, invocationId)
  );
  const previous = existing ? tickForCall(existing) : undefined;
  const tick: {
    kind: string;
    phase?: "task_scope";
    step_seq: number | null;
    agent_rounds?: AgentLlmRound[];
    tool_calls?: AgentToolCall[];
  } = {
    ...previous,
    kind: role === "reviewer"
      ? event.payload.phase === "task_scope" ? "task_scope" : "reviewer_decision"
      : role === "planner" ? "planner_decision" : "executor_tick",
    ...(event.payload.phase === "task_scope" ? { phase: "task_scope" as const } : {}),
    step_seq: event.step_seq ?? null,
  };
  if (event.kind === "agent_llm_round_finished") {
    tick.agent_rounds = upsertLlmRound(tick.agent_rounds, payload);
  } else {
    tick.tool_calls = upsertToolCall(tick.tool_calls, payload);
  }
  return upsertRoleCall(current, {
    step_seq: event.step_seq ?? null,
    reviewer: role === "reviewer" ? tick : null,
    planner: role === "planner" ? tick : null,
    executor: role === "executor" ? tick : null,
  } as Step, role, null);
}

/**
 * Derive Console navigation rows from folded timeline steps.
 * Mirrors Python `expand_role_calls`: Reviewer, Planner, then Executor;
 * loop-only steps skipped.
 */
export function expandRoleCalls(steps: Step[]): RoleCall[] {
  const calls: RoleCall[] = [];
  for (const step of steps) {
    const hasReviewer = !!step.reviewer;
    const hasPlanner = !!step.planner;
    const hasExecutor = !!step.executor;
    if (!hasReviewer && !hasPlanner && !hasExecutor) continue;
    const seq = step.step_seq ?? null;
    const observation = step.observation ?? null;
    if (hasReviewer) {
      const metrics = usageMetrics(step.reviewer!.agent_rounds);
      calls.push({
        call_key: callKeyFor(seq, "reviewer"),
        step_seq: seq,
        role: "reviewer",
        phase: phaseFor({ role: "reviewer", reviewer: step.reviewer }),
        observation,
        reviewer: step.reviewer!,
        planner: null,
        executor: null,
        elapsed_ms: step.reviewer!.role_elapsed_ms ?? null,
        llm_elapsed_ms: metrics.llm_latency_ms_total,
        metrics,
        wall_started_at: step.wall_started_at ?? null,
      });
    }
    if (hasPlanner) {
      const metrics = usageMetrics(step.planner!.agent_rounds);
      calls.push({
        call_key: callKeyFor(seq, "planner"),
        step_seq: seq,
        role: "planner",
        phase: "planning",
        observation,
        reviewer: null,
        planner: step.planner!,
        executor: null,
        elapsed_ms: step.planner!.role_elapsed_ms ?? null,
        llm_elapsed_ms: metrics.llm_latency_ms_total,
        metrics,
        wall_started_at: step.wall_started_at ?? null,
      });
    }
    if (hasExecutor) {
      const metrics = usageMetrics(step.executor!.agent_rounds);
      calls.push({
        call_key: callKeyFor(seq, "executor"),
        step_seq: seq,
        role: "executor",
        phase: "execution",
        observation,
        reviewer: null,
        planner: null,
        executor: step.executor!,
        elapsed_ms: step.executor!.role_elapsed_ms ?? null,
        llm_elapsed_ms: metrics.llm_latency_ms_total,
        metrics,
        wall_started_at: step.wall_started_at ?? null,
      });
    }
  }
  return calls;
}

/** Prefer the persisted trace-ordered calls; folded steps are legacy fallback only. */
export function resolveRoleCalls(
  steps: Step[],
  persisted: RoleCall[] | undefined,
): RoleCall[] {
  if (!persisted) return expandRoleCalls(steps);
  let changed = false;
  const resolved = persisted.map((call) => {
    const metrics = call.metrics ?? usageMetrics(roundsForCall(call));
    const tick = call.role === "reviewer"
      ? call.reviewer
      : call.role === "planner"
        ? call.planner
        : call.executor;
    const phase = call.phase ?? phaseFor(call);
    const elapsedMs = call.elapsed_ms ?? tick?.role_elapsed_ms ?? null;
    const llmElapsedMs = call.llm_elapsed_ms ?? metrics.llm_latency_ms_total;
    if (
      call.phase === phase &&
      call.elapsed_ms === elapsedMs &&
      call.llm_elapsed_ms === llmElapsedMs &&
      call.metrics === metrics
    ) {
      return call;
    }
    changed = true;
    return {
      ...call,
      phase,
      elapsed_ms: elapsedMs,
      llm_elapsed_ms: llmElapsedMs,
      metrics,
    };
  });
  return changed ? resolved : persisted;
}

export type CallRow = {
  call: RoleCall;
  label: string;
  role: "R" | "P" | "E";
  phase: string;
};

export function roundsForCall(call: RoleCall) {
  const tick = call.role === "reviewer"
    ? call.reviewer
    : call.role === "planner"
      ? call.planner
      : call.executor;
  return tick?.agent_rounds ?? [];
}

/** Deduplicated live-safe model totals from the canonical call stream. */
export function aggregateCallMetrics(calls: RoleCall[]): UsageMetrics {
  const seen = new Set<string>();
  const rounds = calls.flatMap(roundsForCall).filter((round) => {
    const key = round.round_id || `${round.invocation_id}:${round.order}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return usageMetrics(rounds);
}

/** Canonical deep-link fragment for the outer role call, when recorded. */
export function agentCallHash(call: RoleCall): string | null {
  const invocationId = roundsForCall(call)[0]?.invocation_id;
  return invocationId ? `#agent-call-${invocationId}` : null;
}

/** Resolve an invocation fragment to its containing focused-role call. */
export function callKeyForAgentHash(
  calls: RoleCall[],
  hash: string,
): string | null {
  const prefix = "#agent-call-";
  if (!hash.startsWith(prefix)) return null;
  const invocationId = hash.slice(prefix.length);
  if (!invocationId) return null;
  return calls.find((call) =>
    roundsForCall(call).some((round) => round.invocation_id === invocationId)
  )?.call_key ?? null;
}

/** Global chronological labels over the exact focused-role call sequence. */
export function deriveCallRows(calls: RoleCall[]): CallRow[] {
  const letters = { reviewer: "R", planner: "P", executor: "E" } as const;
  const phaseLabels = {
    scope: "scope",
    boundary: "review",
    planning: "plan",
    execution: "act",
  } as const;
  return calls.map((call, index) => {
    const phase = call.phase ?? phaseFor(call);
    return {
      call,
      label: String(index + 1).padStart(2, "0"),
      role: letters[call.role],
      phase: phase ? phaseLabels[phase] : "call",
    };
  });
}
