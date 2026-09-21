import { ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AgentCallsBlock } from "@/components/AgentCallsBlock";
import type { ConversationVisualSelectionProps } from "@/lib/agentCalls";
import type { ReviewerTick } from "@/api/types";

export function ReviewerPanel({
  tick,
  taskId,
  selectedVisualKey,
  onSelectVisual,
}: { tick: ReviewerTick; taskId: string } & ConversationVisualSelectionProps) {
  const verdict = tick.decision?.decision;
  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-amber">
            <ShieldCheck className="h-3.5 w-3.5" /> reviewer
          </CardTitle>
          {verdict && (
            <Badge variant="outline" className="border-amber/40 bg-amber/10 font-mono text-[10px] text-amber">
              {verdict}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3 font-mono text-xs">
        {!!tick.observation_ids?.length && (
          <p className="break-all text-text-mute">observations: {tick.observation_ids.join(", ")}</p>
        )}
        {tick.reason && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">reason</div>
            <div className="text-text">{tick.reason}</div>
          </div>
        )}






        <AgentCallsBlock
          taskId={taskId}
          rounds={tick.agent_rounds}
          calls={tick.tool_calls}
          scopeKey={`reviewer:${tick.step_seq ?? "null"}`}
          selectedVisualKey={selectedVisualKey}
          onSelectVisual={onSelectVisual}
        />
      </CardContent>
    </Card>
  );
}
