import { ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AgentCallsBlock } from "@/components/AgentCallsBlock";
import type { ReviewerTick } from "@/api/types";

export function ReviewerPanel({ tick }: { tick: ReviewerTick }) {
  const progress = tick.accepted_progress ?? [];
  const facts = tick.remembered_facts ?? [];
  const answers = tick.answers ?? [];
  const scope = tick.kind === "task_scope";
  const requirements = [
    ["must happen", tick.contract?.must_happen ?? []],
    ["final UI state", tick.contract?.final_ui_state ?? []],
    ["answer", tick.contract?.answer ?? tick.contract?.final_text_to_user ?? []],
  ] as const;
  const disqualifying = tick.contract?.disqualifying_clauses ?? [];
  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-amber">
            <ShieldCheck className="h-3.5 w-3.5" /> reviewer {scope ? "scope" : "boundary"}
          </CardTitle>
          {scope && (
            <Badge variant="outline" className="border-amber/40 bg-amber/10 font-mono text-[10px] text-amber">
              scope
            </Badge>
          )}
          {tick.verdict && (
            <Badge variant="outline" className="border-amber/40 bg-amber/10 font-mono text-[10px] text-amber">
              {tick.verdict}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3 font-mono text-xs">
        {requirements.map(([label, items]) => items.length > 0 && (
          <div key={label}>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">{label}</div>
            <ol className="space-y-1 text-text">
              {items.map((item, index) => <li key={`${index}:${item}`}>{index + 1}. {item}</li>)}
            </ol>
          </div>
        ))}
        {disqualifying.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">disqualifying clauses</div>
            <ul className="space-y-1 text-err">
              {disqualifying.map((item, index) => (
                <li key={`${index}:${item}`}>• {item}</li>
              ))}
            </ul>
          </div>
        )}
        {tick.reason && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">reason</div>
            <div className="text-text">{tick.reason}</div>
          </div>
        )}
        {progress.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">accepted progress</div>
            <ul className="space-y-1 text-text">
              {progress.map((item, index) => (
                <li key={`${index}:${item.statement}`}>✓ {item.statement}</li>
              ))}
            </ul>
          </div>
        )}
        {facts.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">remembered facts</div>
            <ul className="space-y-1 text-text">
              {facts.map((item) => <li key={item.key}>{item.key}: {item.value}</li>)}
            </ul>
          </div>
        )}
        {answers.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">answers</div>
            <ul className="space-y-1 text-text">
              {answers.map((item) => (
                <li key={item.requirement_ref}>{item.requirement_ref}: {item.text}</li>
              ))}
            </ul>
          </div>
        )}
        {tick.final_text_to_user && answers.length === 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-text-mute">answer</div>
            <div className="text-text">{tick.final_text_to_user}</div>
          </div>
        )}
        <AgentCallsBlock
          rounds={tick.agent_rounds}
          calls={tick.tool_calls}
          scopeKey={`reviewer:${scope ? "scope" : tick.step_seq ?? "null"}`}
        />
      </CardContent>
    </Card>
  );
}
