import { Bot } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AgentCallsBlock } from "@/components/AgentCallsBlock";
import type { ConversationVisualSelectionProps } from "@/lib/agentCalls";
import type { Action, ActionPipeline, ExecutorTick } from "@/api/types";

function ParamTable({ action }: { action: Action }) {
  const entries = Object.entries(action).filter(
    ([, v]) => v !== null && v !== undefined && v !== ""
  );
  if (entries.length === 0) return null;
  return (
    <table className="w-full border-collapse font-mono text-xs">
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k} className="border-b border-border last:border-0">
            <td className="w-28 py-1 pr-2 text-[10px] uppercase tracking-wide text-text-mute">
              {k}
            </td>
            <td className="py-1 text-cyan">{String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ActionPipelineView({ pipeline }: { pipeline: ActionPipeline }) {
  return (
    <details className="rounded border border-border bg-bg-2 p-2">
      <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wide text-text-mute">
        action pipeline · {pipeline.origin}
        {pipeline.fallback_reason ? ` · ${pipeline.fallback_reason}` : ""}
      </summary>
      <div className="mt-2 space-y-2">
        <div className="text-text-mute">
          basis: {pipeline.basis_observation_id || "missing"}
          {pipeline.coordinate_space_id ? ` · ${pipeline.coordinate_space_id}` : ""}
        </div>
        {(pipeline.stages ?? []).map((stage, index) => (
          <div key={`${stage.stage}-${index}`} className="border-l border-cyan/30 pl-2">
            <div className="text-[10px] uppercase text-text-mute">
              {stage.stage} · {stage.coordinate_space} · {stage.reason}
            </div>
            <div className="text-cyan">
              {stage.action.type}
              {stage.action.x != null && stage.action.y != null
                ? ` (${stage.action.x}, ${stage.action.y})`
                : stage.action.index != null ? ` [${stage.action.index}]` : ""}
            </div>
          </div>
        ))}
        {pipeline.original_frame_size?.length === 2 && (
          <div className="text-text-mute">
            frame: {pipeline.compressed_frame_size?.join("×") || "—"} → {pipeline.original_frame_size.join("×")}
          </div>
        )}
        {pipeline.validation_result?.reason != null && (
          <div className="text-amber">
            validation: {String(pipeline.validation_result.reason)}
          </div>
        )}
      </div>
    </details>
  );
}

/**
 * Executor body for a single step. The shared observation (SoM + tree) is
 * rendered by StepInspector at the top of the layout — this panel shows
 * subgoal (header-level) plus action / result + LLM I/O.
 */
export function ExecutorPanel({
  tick,
  taskId,
  selectedVisualKey,
  onSelectVisual,
}: { tick: ExecutorTick; taskId: string } & ConversationVisualSelectionProps) {
  const ar = tick.action_result;
  const receipt = ar?.receipt;
  const submitted = tick.tool_calls?.slice().reverse().find(
    (call) => call.name === "submit_executor_step" && call.status === "succeeded",
  )?.arguments.decision;
  const decision = typeof submitted === "string" ? submitted : tick.decision;
  const subgoal = (tick.subgoal_at_tick || "").trim();
  const budget = tick.runtime_budget;
  const budgetLabel = budget?.prediction_rounds != null
    ? `${budget.prediction_rounds} prediction rounds left`
    : budget?.device_actions != null
      ? `${budget.device_actions} actions left`
      : budget?.executor_decisions != null
        ? `${budget.executor_decisions} decisions left`
        : null;
  const visualBasisAction = tick.action != null && [
    "tap", "tap_xy", "swipe", "long_press", "drag",
  ].includes(tick.action.type);
  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-cyan">
            <Bot className="h-3.5 w-3.5" /> executor
          </CardTitle>
          {decision && (
            <Badge
              variant="outline"
              className="border-cyan/40 bg-cyan/10 font-mono text-[10px] text-cyan"
            >
              {decision}
            </Badge>
          )}
          {budgetLabel && (
            <Badge
              variant="outline"
              className="font-mono text-[10px] text-text-mute"
              title={budget?.prediction_rounds != null
                ? budget.prediction_round_accounting || "Remaining prediction rounds"
                : "deterministic runtime capacity; not a semantic completion signal"}
            >
              {budgetLabel}
            </Badge>
          )}
        </div>
        {subgoal && (
          <div className="mt-2 font-mono text-xs leading-snug text-cyan">
            <span className="mr-2 text-[10px] uppercase tracking-wide text-text-mute">
              subgoal
            </span>
            {subgoal}
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-3 font-mono text-xs">
        {tick.action && (
          <div className="space-y-1">
            <div className="text-[10px] uppercase tracking-wide text-text-mute">
              action
            </div>
            <ParamTable action={tick.action} />
          </div>
        )}
        {tick.action_pipeline && <ActionPipelineView pipeline={tick.action_pipeline} />}
        {visualBasisAction && !tick.action_pipeline && (
          <div className="text-text-mute">
            basis: {tick.basis_observation_id || "missing"}
          </div>
        )}
        {tick.summary && (
          <div className="space-y-1">
            <span className="text-[10px] uppercase tracking-wide text-text-mute">
              summary
            </span>
            <div className="text-text">{tick.summary}</div>
          </div>
        )}
        <AgentCallsBlock
          taskId={taskId}
          rounds={tick.agent_rounds}
          calls={tick.tool_calls}
          scopeKey={`executor:${tick.step_seq ?? "null"}`}
          selectedVisualKey={selectedVisualKey}
          onSelectVisual={onSelectVisual}
        />
        {ar && (
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wide text-text-mute">
                action_result
              </span>
              <Badge
                variant="outline"
                className={
                  ar.success
                    ? "border-neon/40 bg-neon/10 font-mono text-[10px] text-neon"
                    : "border-err/40 bg-err/10 font-mono text-[10px] text-err"
                }
              >
                {ar.success ? "success" : "fail"}
              </Badge>
            </div>
            {ar.message && (
              <div className="text-text">{ar.message}</div>
            )}
            {receipt && (
              <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-2 gap-y-1 rounded border border-border bg-bg-2 p-2 text-[10px]">
                <span className="uppercase text-text-mute">effect</span>
                <span className={receipt.effect_outcome === "confirmed"
                  ? "text-neon"
                  : receipt.effect_outcome === "failed" || receipt.effect_outcome === "timeout"
                    ? "text-err"
                    : "text-amber"}
                >
                  {receipt.effect_outcome} · {receipt.effect_class}
                </span>
                <span className="uppercase text-text-mute">observe</span>
                <span className="text-text">
                  {receipt.observation_accepted ? "accepted" : "not accepted"}
                  {` · ${receipt.observation_capture_count} capture(s)`}
                </span>
                {receipt.effect_reason && (
                  <>
                    <span className="uppercase text-text-mute">reason</span>
                    <span className="break-words text-text">{receipt.effect_reason}</span>
                  </>
                )}
              </div>
            )}
            {ar.detail && Object.keys(ar.detail).length > 0 && (
              <pre className="mt-1 max-h-40 overflow-auto rounded border border-border bg-bg-2 p-2 font-mono text-[11px]">
                {JSON.stringify(ar.detail, null, 2)}
              </pre>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
