import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, Bot, Layers } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SemanticTree } from "@/components/SemanticTree";
import { PlannerPanel } from "@/components/PlannerPanel";
import { ReviewerPanel } from "@/components/ReviewerPanel";
import { ExecutorPanel } from "@/components/ExecutorPanel";
import { getArtifactJson } from "@/api/client";
import type { ConversationVisualSelectionProps } from "@/lib/agentCalls";
import { formatMs, formatWallTime } from "@/lib/utils";
import type { RoleCall, RoleCallRole, Step, TreeRefPayload } from "@/api/types";
/* -------------------------------------------------------------------------- */
/*  Observation (top section — shared by focused roles)                       */
/* -------------------------------------------------------------------------- */
function ObservationSection({ step, taskId }: {
    step: Step;
    taskId: string;
}) {
    const obs = step.observation;
    const action = step.executor?.action ?? null;
    const targetIndex = action?.index ?? null;
    // Tree is shared: fetch via the observation's tree_ref if present;
    // otherwise try executor's tree_ref (A1 contract falls back there).
    const treeRef = obs?.tree_ref ?? step.executor?.tree_ref ?? null;
    const tree = useQuery<TreeRefPayload>({
        queryKey: ["artifact", taskId, treeRef],
        queryFn: () => getArtifactJson<TreeRefPayload>(treeRef!, taskId),
        enabled: !!treeRef,
        staleTime: Infinity,
    });
    const tier = tree.data?.filter_tier ?? null;
    const textForLlm = tree.data?.text_for_llm ?? null;
    // live-screen-mirror: SoM image no longer lives here — it migrated to
    // MirrorPanel's Frame mode (D7). ObservationSection is now a folded
    // SemanticTree + exact capture-gap badges.
    const gap = obs?.gap_reasons ?? [];
    // console-ui-quirk-fixes (D2): the summary label reads "N 个元素". The
    // artifact's `elements_count` is primary; the rendered line count is the
    // fallback because that field is unreliable across tiers (live-screen-
    // mirror D8). Both are 0 when no tree is recorded, which keeps the "—".
    const treeLines = textForLlm ? textForLlm.split("\n").length : 0;
    const elementCount = tree.data?.elements_count || treeLines;
    const hasAnyObs = !!treeRef || gap.length > 0;
    if (!hasAnyObs) {
        return (<Card className="border-border">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-text-mute">
            <Layers className="h-3.5 w-3.5"/> observation
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="font-mono text-xs text-text-mute">
            — no observation recorded for this step
          </div>
        </CardContent>
      </Card>);
    }
    return (<Card className="border-border">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-text-mute">
            <Layers className="h-3.5 w-3.5"/> observation
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Exact capture-gap badges. */}
        <div className="flex flex-wrap items-center gap-2">
          {gap.length > 0 && (<div className="flex flex-wrap gap-1">
              {gap.map((g) => (<Badge key={g} variant="outline" className="border-amber/60 bg-amber/10 font-mono text-[10px] text-amber">
                  <AlertTriangle className="mr-1 h-2.5 w-2.5"/>
                  {g}
                </Badge>))}
            </div>)}
        </div>

        {/* live-screen-mirror (D8a/D8b/D26): SemanticTree is the only content
            in ObservationSection now, wrapped in a single default-collapsed
            `<details>` so the inspector stays scannable. The tree body itself
            is horizontally scrollable so long lines don't push the column
            wider. SemanticTree no longer carries its own <details>; the host
            element owns folding. */}
        {/* console-ui-quirk-fixes (D1): `min-w-0` on the wrapper and on the
            <details> completes the chain from the middle Card down to the
            tree's own scroll container, so expanding the fold can never
            widen the inspector column. */}
        <div className="min-w-0 space-y-2">
          <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-wide text-text-mute">
            <Activity className="h-3 w-3"/> 语义树
          </div>
          {/* Uncontrolled: defaults collapsed; survives silent SSE re-renders.
            Parent keys ObservationSection by call_key so a new selection
            remounts collapsed. */}
          <details className="group min-w-0 rounded border border-border bg-bg-1">
            <summary className="flex cursor-pointer items-center gap-2 px-2 py-1 font-mono text-[10px] uppercase tracking-wide text-text-mute hover:bg-bg-2">
              <span className="inline-block transition-transform group-open:rotate-90">
                ▶
              </span>
              <span>{elementCount > 0 ? `${elementCount} 个元素` : "—"}</span>
            </summary>
            <div className="border-t border-border p-2">
              <SemanticTree textForLlm={textForLlm} targetIndex={targetIndex} tier={tier}/>
            </div>
          </details>
        </div>
      </CardContent>
    </Card>);
}
/* -------------------------------------------------------------------------- */
/*  Decision section (middle — role-exclusive)                                 */
/* -------------------------------------------------------------------------- */
/**
 * timeline-role-call-expand: branch strictly on the selected call's role.
 * Co-located role ticks on one step_seq remain separate calls.
 */
function DecisionSection({ role, step, taskId, selectedVisualKey, onSelectVisual, }: {
    role: RoleCallRole;
    step: Step;
    taskId: string;
} & ConversationVisualSelectionProps) {
    if (role === "reviewer" && step.reviewer) {
        return <ReviewerPanel tick={step.reviewer} taskId={taskId} selectedVisualKey={selectedVisualKey} onSelectVisual={onSelectVisual}/>;
    }
    if (role === "planner" && step.planner) {
        return <PlannerPanel tick={step.planner} taskId={taskId} selectedVisualKey={selectedVisualKey} onSelectVisual={onSelectVisual}/>;
    }
    if (role === "executor" && step.executor) {
        return <ExecutorPanel tick={step.executor} taskId={taskId} selectedVisualKey={selectedVisualKey} onSelectVisual={onSelectVisual}/>;
    }
    return (<Card className="border-border">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-text-mute">
          <Bot className="h-3.5 w-3.5"/> decision
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="font-mono text-xs text-text-mute">
          — no focused-role decision recorded for this call
        </div>
      </CardContent>
    </Card>);
}
/* -------------------------------------------------------------------------- */
/*  Step header                                                                */
/* -------------------------------------------------------------------------- */
function StepHeader({ call, }: {
    call: RoleCall;
}) {
    const seq = call.step_seq;
    const roleName = call.role === "reviewer"
        ? "Reviewer"
        : call.role === "planner"
            ? "Planner"
            : "Executor";
    const phase = call.phase ?? (call.role === "reviewer" ? "boundary"
        : call.role === "planner" ? "planning" : "execution");
    const metrics = call.metrics;
    const mode = call.observation?.observation_mode ?? call.executor?.observation_mode ?? null;
    const count = (value?: number | null) => value == null ? "—" : value.toLocaleString();
    const cache = metrics?.cache_read_ratio == null
        ? "—"
        : `${Math.round(metrics.cache_read_ratio * 100)}%`;
    const cacheUsage = metrics?.cache_read_tokens_total == null
        ? "—"
        : `${count(metrics.cache_read_tokens_total)} (${cache})`;
    return (<div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-text-mute">
        <Badge variant="outline" className="font-mono text-[10px] text-text">
          {roleName}
        </Badge>
        <span className="text-text">{phase}</span>
        <span className="text-text-mute" title="device-action position">
          action {seq == null ? "—" : String(seq).padStart(2, "0")}
        </span>
        <span className="inline-flex h-5 items-center gap-1 rounded border border-border bg-bg-2 px-1.5 text-text tabular-nums" title="earliest persisted wall-clock event at this action position">
          started {formatWallTime(call.wall_started_at)}
        </span>
        <span className="inline-flex h-5 items-center gap-1 rounded border border-cyan/40 bg-cyan/10 px-1.5 text-cyan tabular-nums" title="full focused-role invocation elapsed (monotonic)">
          call {formatMs(call.elapsed_ms)}
        </span>
      </div>
      <div className="flex items-center gap-1.5 overflow-x-auto pb-0.5 font-mono text-[10px] uppercase tracking-wider text-text-mute">
        <span className="inline-flex h-5 shrink-0 items-center gap-1 rounded border border-violet/40 bg-violet/10 px-1.5 text-violet tabular-nums" title="sum of this call's provider round latency">
          LLM {formatMs(call.llm_elapsed_ms ?? metrics?.llm_latency_ms_total)}
        </span>
        <MetaChip label="mode" value={mode} accent="cyan"/>
        <MetaChip label="input" value={count(metrics?.input_tokens_total)} accent="violet"/>
        <MetaChip label="cache" value={cacheUsage} accent="violet"/>
        <MetaChip label="output" value={count(metrics?.output_tokens_total)} accent="violet"/>
      </div>
    </div>);
}
/** Compact metadata value used only by the selected-call summary. */
function MetaChip({ label, value, accent, }: {
    label: string;
    value: string | null | undefined;
    accent: "cyan" | "violet" | "amber";
}) {
    const accentClass = {
        cyan: "border-cyan/40 bg-cyan/10 text-cyan",
        violet: "border-violet/40 bg-violet/10 text-violet",
        amber: "border-amber/40 bg-amber/10 text-amber",
    }[accent];
    return (<span className={`inline-flex h-5 min-w-[2rem] shrink-0 items-center gap-1 rounded border px-1.5 font-mono text-[10px] tabular-nums ${accentClass}`}>
      <span className="opacity-70">{label}</span>
      <span>{value ?? "—"}</span>
    </span>);
}
/* -------------------------------------------------------------------------- */
/*  Top-level StepInspector                                                    */
/* -------------------------------------------------------------------------- */
/**
 * timeline-role-call-expand: inspector is driven by a selected RoleCall.
 * Decision and observation content come only from the selected canonical call.
 */
function viewStepFromCall(call: RoleCall): Step {
    return {
        step_seq: call.step_seq,
        observation: call.observation ?? null,
        reviewer: call.role === "reviewer" ? call.reviewer : null,
        planner: call.role === "planner" ? call.planner : null,
        executor: call.role === "executor" ? call.executor : null,
    };
}
function ActiveSkills({ call }: {
    call: RoleCall;
}) {
    const skills = call.role === "reviewer"
        ? call.reviewer?.active_skills
        : call.role === "planner"
            ? call.planner?.active_skills
            : call.executor?.active_skills;
    if (!skills?.length)
        return null;
    return (<details className="rounded border border-border bg-bg-2 p-2 font-mono">
      <summary className="cursor-pointer text-[10px] uppercase tracking-wide text-text-mute">
        active skills · {skills.length}
      </summary>
      <pre className="mt-2 whitespace-pre-wrap text-[11px] text-text">
        {JSON.stringify(skills, null, 2)}
      </pre>
    </details>);
}
export function StepInspector({ call, taskId, selectedVisualKey, onSelectVisual, }: {
    call: RoleCall;
    taskId: string;
} & ConversationVisualSelectionProps) {
    const step = viewStepFromCall(call);
    return (<div className="space-y-4">
      <StepHeader call={call}/>
      <DecisionSection role={call.role} step={step} taskId={taskId} selectedVisualKey={selectedVisualKey} onSelectVisual={onSelectVisual}/>
      <ActiveSkills call={call}/>
      {(<ObservationSection key={call.call_key} step={step} taskId={taskId}/>)}
    </div>);
}
