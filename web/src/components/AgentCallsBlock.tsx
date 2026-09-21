import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Clipboard } from "lucide-react";

import type { AgentLlmRound, AgentToolCall } from "@/api/types";
import { getArtifactText } from "@/api/client";
import { cn, formatMs } from "@/lib/utils";
import {
  agentFoldKey,
  buildAgentCallRows,
  cacheSummary,
  collapsedModelInputSummary,
  conversationArtifactEnabled,
  conversationArtifactQueryKey,
  type ConversationVisualSelectionProps,
  conversationFoldOpen,
  inputSectionFoldKey,
  modelConversationVisuals,
  observationTransitionLabel,
  projectInputSections,
  type ArtifactView,
  visibleArtifactPayload,
} from "@/lib/agentCalls";

function pretty(value: unknown): string {
  if (typeof value !== "string") return JSON.stringify(value ?? null, null, 2);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function FoldBlock({
  foldKey,
  label,
  summary,
  open,
  setOpen,
  value,
  refId,
  artifactView = "raw",
  error,
  loading = false,
  selected = false,
  onSelect,
  taskId,
}: {
  foldKey: string;
  label: string;
  summary: string;
  open: boolean;
  setOpen: (key: string, value: boolean) => void;
  value?: unknown;
  refId?: string | null;
  artifactView?: ArtifactView;
  error?: string | null;
  loading?: boolean;
  selected?: boolean;
  onSelect?: () => void;
  taskId: string;
}) {
  const artifact = useQuery({
    queryKey: conversationArtifactQueryKey(taskId, refId),
    queryFn: () => getArtifactText(refId!, taskId),
    enabled: conversationArtifactEnabled(open, refId),
    staleTime: Infinity,
  });
  const text = useMemo(() => {
    if (artifact.data != null) return pretty(visibleArtifactPayload(artifact.data, artifactView));
    return pretty(value);
  }, [artifact.data, artifactView, value]);

  return (
    <div
      data-fold-key={foldKey}
      className={cn(
        "rounded border border-border bg-bg-1",
        selected && "border-cyan ring-1 ring-cyan/60",
      )}
    >
      <button
        type="button"
        aria-pressed={onSelect ? selected : undefined}
        onClick={() => {
          onSelect?.();
          setOpen(foldKey, !open);
        }}
        className="flex w-full min-w-0 items-center gap-2 px-2 py-1.5 text-left hover:bg-bg-2"
      >
        <ChevronRight className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")} />
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wide text-text">{label}</span>
        <span className={cn("min-w-0 truncate text-[10px]", error ? "text-err" : "text-text-mute")}>{error || summary}</span>
      </button>
      {open && (
        <div className="relative border-t border-border bg-bg-2">
          <button
            type="button"
            title="Copy"
            onClick={() => void navigator.clipboard?.writeText(text)}
            className="absolute right-2 top-2 z-10 rounded border border-border bg-bg-1 p-1 text-text-mute hover:text-text"
          >
            <Clipboard className="h-3 w-3" />
          </button>
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words p-2 pr-9 font-mono text-[11px] leading-5 text-text">
            {loading || artifact.isLoading ? "loading…" : artifact.error ? `error: ${String(artifact.error)}` : text}
          </pre>
        </div>
      )}
    </div>
  );
}

function InputFoldBlock({
  foldKey,
  label,
  summary,
  value,
  open,
  folds,
  setOpen,
  loading = false,
  error,
  selected = false,
  onSelect,
  refId,
  taskId,
}: {
  foldKey: string;
  label: string;
  summary: string;
  value?: unknown;
  open: boolean;
  folds: Record<string, boolean>;
  setOpen: (key: string, value: boolean) => void;
  loading?: boolean;
  error?: string | null;
  selected?: boolean;
  onSelect?: () => void;
  refId?: string | null;
  taskId: string;
}) {
  const artifact = useQuery({
    queryKey: conversationArtifactQueryKey(taskId, refId),
    queryFn: () => getArtifactText(refId!, taskId),
    enabled: conversationArtifactEnabled(open, refId),
    staleTime: Infinity,
  });
  const artifactValue = artifact.data == null
    ? undefined
    : visibleArtifactPayload(artifact.data, "request");
  const resolvedValue = artifactValue ?? value;
  const sections = useMemo(
    () => resolvedValue === undefined ? [] : projectInputSections(resolvedValue),
    [resolvedValue],
  );
  const resolvedLoading = loading || (open && !!refId && artifact.isLoading);
  const resolvedError = error || (artifact.error ? String(artifact.error) : null);
  return (
    <div
      data-fold-key={foldKey}
      className={cn(
        "rounded border border-border bg-bg-1",
        selected && "border-cyan ring-1 ring-cyan/60",
      )}
    >
      <button
        type="button"
        aria-pressed={onSelect ? selected : undefined}
        onClick={() => {
          onSelect?.();
          setOpen(foldKey, !open);
        }}
        className="flex w-full min-w-0 items-center gap-2 px-2 py-1.5 text-left hover:bg-bg-2"
      >
        <ChevronRight className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")} />
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wide text-text">{label}</span>
        <span className={cn("min-w-0 truncate text-[10px]", resolvedError ? "text-err" : "text-text-mute")}>
          {resolvedError || summary}
        </span>
      </button>
      {open && (
        <div className="space-y-1 border-t border-border bg-bg-2 p-1.5">
          {resolvedLoading && <div className="px-2 py-1 font-mono text-[10px] text-text-mute">loading…</div>}
          {resolvedError && <div className="px-2 py-1 font-mono text-[10px] text-err">error: {resolvedError}</div>}
          {!resolvedLoading && !resolvedError && sections.map((section, index) => {
            const sectionKey = inputSectionFoldKey(foldKey, section, index);
            const sectionValue = section.items.length === 1
              ? section.items[0].content
              : section.items.map((item) => item.content);
            const roles = [...new Set(section.items.map((item) => item.role).filter(Boolean))];
            return (
              <FoldBlock
                key={sectionKey}
                foldKey={sectionKey}
                label={section.label}
                summary={`${roles.join("/") || "message"} · ${section.characterCount.toLocaleString()} chars`}
                value={sectionValue}
                open={!!folds[sectionKey]}
                setOpen={setOpen}
                onSelect={onSelect}
                taskId={taskId}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function roundMeta(round: AgentLlmRound): string {
  const usage = cacheSummary(round);
  const tokens = usage.input == null && usage.output == null
    ? "tokens —"
    : `${usage.input ?? "—"} in · ${usage.output ?? "—"} out`;
  const cache = usage.available && usage.hitRatio != null
    ? ` · cache ${Math.round(usage.hitRatio * 100)}%`
    : "";
  const reasoning = usage.reasoning == null ? "" : ` · reasoning ${usage.reasoning}`;
  return `${round.model || "model"} · ${formatMs(round.latency_ms)} · ${tokens}${cache}${reasoning}`;
}

export function AgentCallsBlock({
  rounds,
  calls,
  scopeKey = "call",
  selectedVisualKey,
  onSelectVisual,
  taskId,
}: {
  rounds?: AgentLlmRound[];
  calls?: AgentToolCall[];
  scopeKey?: string;
  taskId: string;
} & ConversationVisualSelectionProps) {
  const rows = buildAgentCallRows(rounds ?? [], calls ?? []);
  const modelRows = rows.filter((row) => row.kind === "model").map((row) => row.round);
  const invocationId = modelRows[0]?.invocation_id;
  const [folds, setFolds] = useState<Record<string, boolean>>({});
  if (rows.length === 0) return null;

  const setOpen = (foldKey: string, value: boolean) => {
    setFolds((current) => ({ ...current, [foldKey]: value }));
  };

  return (
    <section
      id={invocationId ? `agent-call-${invocationId}` : undefined}
      className="scroll-mt-20 space-y-2 rounded border border-border bg-bg-2 p-2"
    >
      <div className="font-mono text-[10px] uppercase tracking-wide text-text-mute">Agent conversation</div>
      {rows.map((row) => {
        if (row.kind === "model") {
          const round = row.round;
          const roundIndex = modelRows.findIndex((candidate) => candidate.round_id === round.round_id);
          const isInitialInput = roundIndex === 0;
          const inputKey = agentFoldKey(scopeKey, round, "input");
          const modelVisuals = modelConversationVisuals(scopeKey, round);
          const outputKey = agentFoldKey(scopeKey, round, "output");
          const diagnosticsKey = agentFoldKey(scopeKey, round, "diagnostics");
          const reasoningKey = `${outputKey}:reasoning`;
          const roundCalls = (calls ?? []).filter((call) => (
            call.invocation_id === round.invocation_id
            && call.llm_round_order === round.order
          ));
          const calledTools = [...new Set(roundCalls.map((call) => call.name))];
          const outputSummary = calledTools.length > 0
            ? `${roundMeta(round)} · calls ${calledTools.join(", ")}`
            : `${roundMeta(round)} · ${round.stream_status || round.stop_reason || "response"}`;
          const isLatestOutput = roundIndex === modelRows.length - 1;
          return (
            <Fragment key={round.round_id}>
              {(!!round.request_ref || !!modelVisuals) && (
                <div className="relative border-l-2 border-violet/40 pl-3">
                  <span className="absolute -left-[5px] top-3 h-2 w-2 rounded-full bg-violet" />
                  <InputFoldBlock
                    foldKey={inputKey}
                    label={isInitialInput ? "Initial input" : "Model input"}
                    summary={collapsedModelInputSummary(round)}
                    refId={round.request_ref}
                    open={conversationFoldOpen(folds, inputKey)}
                    folds={folds}
                    setOpen={setOpen}
                    taskId={taskId}
                    selected={modelVisuals?.input.rowKey === selectedVisualKey}
                    onSelect={modelVisuals && onSelectVisual
                      ? () => onSelectVisual(modelVisuals.input)
                      : undefined}
                  />
                </div>
              )}
              <div className="relative border-l-2 border-violet/40 pl-3">
                <span className="absolute -left-[5px] top-3 h-2 w-2 rounded-full bg-violet" />
                <FoldBlock
                  foldKey={outputKey}
                  label={round.stream_status ? "Model output · live" : "Model output"}
                  summary={outputSummary}
                  refId={round.response_ref}
                  artifactView="response"
                  value={round.live_output}
                  open={conversationFoldOpen(folds, outputKey, isLatestOutput)}
                  setOpen={setOpen}
                  selected={modelVisuals?.output.rowKey === selectedVisualKey}
                  onSelect={modelVisuals && onSelectVisual
                    ? () => onSelectVisual(modelVisuals.output)
                    : undefined}
                  taskId={taskId}
                />
              </div>
              <div className="ml-3">
                <FoldBlock
                  foldKey={diagnosticsKey}
                  label="Round diagnostics"
                  summary={`${round.message_count ?? "—"} messages · ${round.image_count ?? "—"} images`}
                  value={{
                    usage: round.usage,
                    stable_prefix_hash: round.stable_prefix_hash || null,
                    tool_catalog_hash: round.tool_catalog_hash || null,
                    stop_reason: round.stop_reason,
                  }}
                  open={conversationFoldOpen(folds, diagnosticsKey)}
                  setOpen={setOpen}
                  taskId={taskId}
                />
              </div>
              {round.reasoning_status && round.reasoning_status !== "not_requested" && (
                <div className="relative border-l-2 border-amber/40 pl-3">
                  <span className="absolute -left-[5px] top-3 h-2 w-2 rounded-full bg-amber" />
                  <FoldBlock
                    foldKey={reasoningKey}
                    label="Reasoning diagnostic"
                    summary={`${round.reasoning_status || "supported"}${round.reasoning_effort ? ` · ${round.reasoning_effort}` : ""}`}
                    value={{
                      diagnostic_only: true,
                      status: round.reasoning_status,
                      effort: round.reasoning_effort,
                      summary_preference: round.reasoning_summary_preference,
                      summary: round.reasoning_summary,
                      reasoning_tokens: round.usage?.reasoning_tokens ?? null,
                    }}
                    open={conversationFoldOpen(folds, reasoningKey)}
                    setOpen={setOpen}
                    taskId={taskId}
                  />
                </div>
              )}
            </Fragment>
          );
        }

        const call = row.call;
        const round = modelRows.find((candidate) => (
          candidate.invocation_id === call.invocation_id
          && candidate.order === call.llm_round_order
        ));
        if (!round) return null;
        const argumentsKey = agentFoldKey(scopeKey, round, "tool_arguments", call.call_id);
        const resultKey = agentFoldKey(scopeKey, round, "tool_result", call.call_id);
        const result = call.local_result
          ?? (call.result && Object.keys(call.result).length > 0 ? call.result : call.result_summary);
        return (
          <div key={`${call.invocation_id}:${call.call_id}`} className="relative space-y-1 border-l-2 border-cyan/40 pl-3">
            <span className="absolute -left-[5px] top-3 h-2 w-2 rounded-full bg-cyan" />
            <FoldBlock
              foldKey={argumentsKey}
              label={`Tool input · ${call.name}`}
              summary={Object.keys(call.arguments ?? {}).join(", ") || "no arguments"}
              value={call.arguments}
              open={conversationFoldOpen(folds, argumentsKey)}
              setOpen={setOpen}
              taskId={taskId}
            />
            <FoldBlock
              foldKey={resultKey}
              label={`Tool output · ${call.name}`}
              summary={`${call.status} · ${formatMs(call.elapsed_ms)}`}
              error={call.error}
              value={result}
              open={conversationFoldOpen(folds, resultKey)}
              setOpen={setOpen}
              taskId={taskId}
            />
            {call.observation_transition && (
              <div className="ml-2 border-l border-amber/50 py-1 pl-2 font-mono text-[10px] text-amber">
                {observationTransitionLabel(call.observation_transition)}
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}
