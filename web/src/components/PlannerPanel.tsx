import { Route } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AgentCallsBlock } from "@/components/AgentCallsBlock";
import type { PlannerTick } from "@/api/types";

export function PlannerPanel({ tick }: { tick: PlannerTick }) {
  const plan = tick.plan ?? [];
  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-violet">
            <Route className="h-3.5 w-3.5" /> planner
          </CardTitle>
          {tick.mode && (
            <Badge variant="outline" className="border-violet/40 bg-violet/10 font-mono text-[10px] text-violet">
              {tick.mode}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3 font-mono text-xs">
        {tick.next_subgoal && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">next subgoal</div>
            <div className="text-text">{tick.next_subgoal}</div>
          </div>
        )}
        {plan.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">plan</div>
            <ol className="ml-4 list-decimal space-y-0.5 text-text">
              {plan.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}
            </ol>
          </div>
        )}
        {tick.completion_contract && (
          <details className="rounded border border-border bg-bg-2 p-2">
            <summary className="cursor-pointer text-[10px] uppercase tracking-wide text-text-mute">
              completion contract
            </summary>
            <pre className="mt-2 whitespace-pre-wrap text-[11px] text-text">
              {JSON.stringify(tick.completion_contract, null, 2)}
            </pre>
          </details>
        )}
        <AgentCallsBlock
          rounds={tick.agent_rounds}
          calls={tick.tool_calls}
          scopeKey={`planner:${tick.step_seq ?? "null"}`}
        />
      </CardContent>
    </Card>
  );
}
