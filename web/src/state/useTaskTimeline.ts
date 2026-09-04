import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getTask, getTimeline } from "@/api/client";
import { openTaskStream } from "@/api/sse";
import type {
  ExecutorTick,
  LoopTick,
  PlannerTick,
  ReviewerTick,
  Step,
  TaskTimeline,
  TraceEvent,
} from "@/api/types";
import {
  resolveRoleCalls,
  upsertLiveRoleEvent,
  upsertRoleCall,
} from "@/lib/expandRoleCalls";
import {
  observationFromTracePayload,
  observationRoleWins,
  reconcileObservation,
} from "@/lib/observationReplay";

const timelineKey = (id: string) => ["timeline", id] as const;
const taskKey = (id: string) => ["task", id] as const;

function isRunning(status?: string): boolean {
  return status === "running" || status === "queued";
}

function findOrCreateStep(steps: Step[], seq: number | null | undefined): Step {
  const key = seq ?? null;
  const found = steps.find((s) => (s.step_seq ?? null) === key);
  if (found) return found;
  const created: Step = {
    step_seq: key,
    observation: null,
    reviewer: null,
    planner: null,
    executor: null,
    loop: null,
  };
  steps.push(created);
  return created;
}

function withFocusedDecision(payload: Record<string, unknown>): Record<string, unknown> {
  const decision = payload.decision;
  return decision != null && typeof decision === "object" && !Array.isArray(decision)
    ? { ...payload, ...(decision as Record<string, unknown>) }
    : payload;
}

function withCalls(timeline: TaskTimeline): TaskTimeline {
  return { ...timeline, calls: resolveRoleCalls(timeline.steps, timeline.calls) };
}

/**
 * Fetch a task's timeline. For a running task, also opens an SSE
 * subscription and merges incoming trace events into the cached timeline
 * (step-grouped: events with the same step_seq fold into one Step).
 * The chronological `calls` stream additionally includes Reviewer scope,
 * which is intentionally not folded into a device-action Step.
 */
export function useTaskTimeline(taskId: string | undefined) {
  const qc = useQueryClient();

  const timeline = useQuery<TaskTimeline>({
    queryKey: taskId ? timelineKey(taskId) : ["timeline", "none"],
    queryFn: async () => withCalls(await getTimeline(taskId!)),
    enabled: !!taskId,
  });

  const task = useQuery({
    queryKey: taskId ? taskKey(taskId) : ["task", "none"],
    queryFn: () => getTask(taskId!),
    enabled: !!taskId,
    // Poll running tasks as a fallback alongside SSE.
    refetchInterval: (query) => {
      const data = query.state.data as { status?: string } | undefined;
      return data && isRunning(data.status) ? 2000 : false;
    },
  });

  useEffect(() => {
    if (!taskId) return;
    const current = timeline.data;
    if (!current || current.read_only || !isRunning(current.status)) return;

    const sub = openTaskStream(taskId, {
      onEvent: (event: TraceEvent) => {
        qc.setQueryData<TaskTimeline>(timelineKey(taskId), (old) => {
          if (!old) return old;
          if (
            event.kind === "agent_tool_started" ||
            event.kind === "agent_tool_finished" ||
            event.kind === "agent_tool_failed" ||
            event.kind === "agent_llm_round_finished"
          ) {
            return {
              ...old,
              calls: upsertLiveRoleEvent(
                resolveRoleCalls(old.steps, old.calls),
                event,
              ),
            };
          }
          if (event.kind === "task_scope") {
            const seq = event.step_seq ?? null;
            const scope = {
              ...(event.payload as object),
              kind: "task_scope",
              step_seq: seq,
              message: event.message,
              level: event.level,
            } as ReviewerTick;
            const scopeEnvelope: Step = {
              step_seq: seq,
              observation: null,
              reviewer: scope,
              planner: null,
              executor: null,
              loop: null,
            };
            const calls = upsertRoleCall(
              resolveRoleCalls(old.steps, old.calls),
              scopeEnvelope,
              "reviewer",
              null,
            );
            return { ...old, task_scope: scope, calls };
          }
          // Skip non-tick events for the timeline rail.
          if (
            event.kind !== "reviewer_decision" &&
            event.kind !== "planner_decision" &&
            event.kind !== "executor_tick" &&
            event.kind !== "loop_tick"
          ) {
            return old;
          }
          const steps = [...old.steps];
          const seq = event.step_seq ?? null;
          // loop_tick(N) attaches to step N's loop slot as effective
          // plan/subgoal context between focused-role calls.
          const bucket = findOrCreateStep(steps, seq);
          let calls = resolveRoleCalls(old.steps, old.calls);
          const obsFromSrc = observationFromTracePayload(
            event.payload as Record<string, unknown>,
          );
          if (event.kind === "reviewer_decision") {
            const payload = withFocusedDecision(
              event.payload as Record<string, unknown>,
            );
            bucket.reviewer = {
              ...payload,
              kind: "reviewer_decision",
              step_seq: seq,
              message: event.message,
              level: event.level,
            } as ReviewerTick;
            const wins = observationRoleWins(bucket.observation_role, "reviewer");
            bucket.observation = reconcileObservation(
              bucket.observation,
              obsFromSrc,
              wins,
            );
            if (wins) bucket.observation_role = "reviewer";
            calls = upsertRoleCall(calls, bucket, "reviewer", obsFromSrc);
          } else if (event.kind === "planner_decision") {
            const payload = withFocusedDecision(
              event.payload as Record<string, unknown>,
            );
            bucket.planner = {
              ...payload,
              kind: "planner_decision",
              step_seq: seq,
              message: event.message,
              level: event.level,
            } as PlannerTick;
            const wins = observationRoleWins(bucket.observation_role, "planner");
            bucket.observation = reconcileObservation(
              bucket.observation,
              obsFromSrc,
              wins,
            );
            if (wins) bucket.observation_role = "planner";
            calls = upsertRoleCall(calls, bucket, "planner", obsFromSrc);
          } else if (event.kind === "executor_tick") {
            bucket.executor = {
              ...(event.payload as object),
              kind: "executor_tick",
              step_seq: seq,
              message: event.message,
              level: event.level,
            } as ExecutorTick;
            const wins = observationRoleWins(bucket.observation_role, "executor");
            bucket.observation = reconcileObservation(
              bucket.observation,
              obsFromSrc,
              wins,
            );
            if (wins) bucket.observation_role = "executor";
            calls = upsertRoleCall(calls, bucket, "executor", obsFromSrc);
          } else {
            // loop_tick(N) → step N's effective-context loop slot.
            bucket.loop = {
              ...(event.payload as object),
              kind: "loop_tick",
              step_seq: seq,
              message: event.message,
              level: event.level,
            } as LoopTick;
          }
          // Re-sort: by step_seq ascending; NULL-seq bucket (task-started /
          // task-succeeded boundary ticks with no step number) stays last.
          steps.sort((a, b) => {
            const sa = a.step_seq;
            const sb = b.step_seq;
            if (sa == null && sb == null) return 0;
            if (sa == null) return 1;
            if (sb == null) return -1;
            return sa - sb;
          });
          return { ...old, steps, calls };
        });
        // A terminal event means the task finished; refetch task metadata.
        if (
          event.kind === "loop_tick" &&
          /succeeded|failed|task_cancelled/.test(event.message)
        ) {
          qc.invalidateQueries({ queryKey: taskKey(taskId) });
          qc.invalidateQueries({ queryKey: timelineKey(taskId) });
        }
      },
    });
    return () => sub.close();
    // Re-run when the task transitions into/out of running.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, timeline.data?.status]);

  return { timeline, task };
}
