import { Route } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AgentCallsBlock } from "@/components/AgentCallsBlock";
import type { ConversationVisualSelectionProps } from "@/lib/agentCalls";
import type { PlannerTick } from "@/api/types";

export function PlannerPanel({
  tick,
  taskId,
  selectedVisualKey,
  onSelectVisual,
}: { tick: PlannerTick; taskId: string } & ConversationVisualSelectionProps) {
  const currentPlan = tick.plan;
  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-violet">
            <Route className="h-3.5 w-3.5" /> planner
          </CardTitle>
          {tick.decision?.decision && (
            <Badge variant="outline" className="border-violet/40 bg-violet/10 font-mono text-[10px] text-violet">
              {tick.decision?.decision}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3 font-mono text-xs">
        {tick.reason && <p className="text-text">{tick.reason}</p>}

        {currentPlan?.current_stage && (
          <div className="space-y-2">
            <p>当前目标：{currentPlan.current_stage.goal}</p>
            {!!currentPlan.current_stage.skill_ids?.length && (
              <p className="text-text-mute">阶段技能：{currentPlan.current_stage.skill_ids.join("、")}</p>
            )}
            {currentPlan.assumption_roadmap.length > 0 && (
              <div className="text-text-mute">
                <p>后续设想（待验证，将随执行反馈更新）</p>
                <ul>{currentPlan.assumption_roadmap.map((goal, i) => <li key={i}>{goal}</li>)}</ul>
              </div>
            )}
          </div>
        )}


        <AgentCallsBlock
          taskId={taskId}
          rounds={tick.agent_rounds}
          calls={tick.tool_calls}
          scopeKey={`planner:${tick.step_seq ?? "null"}`}
          selectedVisualKey={selectedVisualKey}
          onSelectVisual={onSelectVisual}
        />
      </CardContent>
    </Card>
  );
}
