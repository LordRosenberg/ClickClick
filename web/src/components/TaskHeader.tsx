import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMs } from "@/lib/utils";
import { aggregateCallMetrics } from "@/lib/expandRoleCalls";
import { RevisablePlanView } from "@/components/RevisablePlanView";
import type { Task, TaskTimeline } from "@/api/types";
const statusVariant: Record<string, "default" | "secondary" | "success" | "destructive" | "neon" | "warn"> = {
  queued: "secondary",
  running: "neon",
  succeeded: "secondary",
  failed: "destructive",
  cancelled: "warn",
};

const statusLabel: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  succeeded: "运行时完成",
  failed: "运行时失败",
  cancelled: "已取消",
};

export function TaskHeader({
  timeline,
  executionElapsedMs,
  liveTask,
}: {
  timeline: TaskTimeline;
  executionElapsedMs?: number | null;
  liveTask?: Task;
}) {
  const step = liveTask?.step_number ?? timeline.step_number ?? 0;
  const runtime = timeline.metrics?.runtime_terminal_status;
  const persistedMetrics = timeline.metrics;
  const callMetrics = aggregateCallMetrics(timeline.calls ?? []);
  const callRoundCount = callMetrics.round_count ?? 0;
  const metrics = callRoundCount >= (persistedMetrics?.round_count ?? 0)
    && callRoundCount > 0
    ? { ...persistedMetrics, ...callMetrics }
    : persistedMetrics ?? callMetrics;
  const runtimeStatus = liveTask?.status ?? runtime?.status ?? timeline.status;
  const revisable = liveTask?.state?.revisable ?? timeline.revisable;
  const nextRole = revisable?.next_role ?? timeline.next_role;
  const historicalRequests = timeline.metrics?.provider_requests
    ? Object.values(timeline.metrics.provider_requests).reduce((sum, value) => sum + value, 0)
    : null;
  const requestCount = liveTask?.state?.role_invocation_count
    ?? timeline.role_invocation_count
    ?? historicalRequests;
  const semanticStatus = runtime?.semantic_true_success ?? "not_assessed";
  const roleCalls = timeline.calls?.length ?? null;
  const models = [...new Set((timeline.calls ?? []).flatMap((call) => (
    call.reviewer?.agent_rounds
    ?? call.planner?.agent_rounds
    ?? call.executor?.agent_rounds
    ?? []
  )).flatMap((round) => round.model ? [round.model] : []))];
  const count = (value?: number | null) => value == null ? "—" : value.toLocaleString();
  const cacheRatio = metrics?.cache_read_ratio == null
    ? "—"
    : `${Math.round(metrics.cache_read_ratio * 100)}%`;

  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-xs uppercase tracking-wider text-text-mute">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-cyan" />
            task header
          </CardTitle>
          <Badge variant={statusVariant[runtimeStatus] ?? "secondary"}>
            runtime: {statusLabel[runtimeStatus] ?? runtimeStatus}
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px] text-text-mute">
            semantic true success: {semanticStatus}
          </Badge>
          {models.length > 0 && (
            <Badge variant="outline" className="font-mono text-[10px] text-text-mute">
              model {models.join(" / ")}
            </Badge>
          )}
          <span className="font-mono text-[10px] uppercase tracking-wider text-text-mute">
            action{" "}
            <span className="text-text">{String(step).padStart(2, "0")}</span>
          </span>
          <span
            className="inline-flex h-5 items-center gap-1 rounded border border-cyan/40 bg-cyan/10 px-1.5 font-mono text-[10px] uppercase tracking-wide text-cyan tabular-nums"
            title="task wall-clock elapsed from creation to terminal update/current time"
          >
            elapsed {formatMs(executionElapsedMs ?? timeline.execution_elapsed_ms)}
          </span>
          <Badge variant="outline" className="font-mono text-[10px] text-text-mute">
            calls {count(roleCalls)} · requests {count(requestCount)} · rounds {count(metrics?.round_count)} · actions {count(step)}
          </Badge>
          {nextRole && runtimeStatus === "running" && (
            <Badge variant="outline" className="font-mono text-[10px] text-cyan">
              next {nextRole}
            </Badge>
          )}
          <Badge variant="outline" className="font-mono text-[10px] text-violet">
            LLM {formatMs(metrics?.llm_latency_ms_total)}
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px] text-violet">
            input {count(metrics?.input_tokens_total)} · cached {count(metrics?.cache_read_tokens_total)} ({cacheRatio}) · output {count(metrics?.output_tokens_total)}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 font-mono">
        <div className="text-[12px] leading-relaxed text-text">
          {timeline.instruction}
        </div>

        {revisable?.plan && <RevisablePlanView runtime={revisable} />}


        {timeline.failure_reason && (
          <div className="rounded border border-err/40 bg-err/10 p-2 font-mono text-[11px] text-err">
            <span className="text-[10px] uppercase tracking-wider">failure:</span>{" "}
            {timeline.failure_reason}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
