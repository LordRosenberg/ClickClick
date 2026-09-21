import type { AgentLlmRound, AgentLlmStream, AgentToolCall, ObservationTransition } from "../api/types.ts";

export function agentFoldKey(
  scope: string, round: AgentLlmRound, blockKind: string, blockId = "main",
): string {
  return `${scope}:${round.invocation_id}:${round.round_id}:${blockKind}:${blockId}`;
}

export function observationTransitionLabel(transition: ObservationTransition): string {
  return `${transition.from_observation_id || "—"} ${transition.from_mode} → ${transition.to_observation_id} ${transition.to_mode}`;
}

export type ConversationVisual = {
  rowKey: string;
  artifactRef: string;
  label: string;
  observationId: string | null;
  capturedAt: number | null;
};

export type ConversationVisualSelectionProps = {
  selectedVisualKey?: string | null;
  onSelectVisual?: (visual: ConversationVisual) => void;
};

export function modelConversationVisuals(
  scope: string,
  round: AgentLlmRound,
): { input: ConversationVisual; output: ConversationVisual } | null {
  if (!round.input_model_image_ref) return null;
  const common = {
    artifactRef: round.input_model_image_ref,
    observationId: round.input_observation_id ?? null,
    capturedAt: round.input_captured_monotonic_ms ?? null,
  };
  return {
    input: {
      ...common,
      rowKey: agentFoldKey(scope, round, "input"),
      label: "Model input",
    },
    output: {
      ...common,
      rowKey: agentFoldKey(scope, round, "output"),
      label: "Model output context",
    },
  };
}

export type ArtifactView = "request" | "response" | "raw";

export type InputSectionItem = {
  role: string | null;
  content: unknown;
};

export type InputSection = {
  label: string;
  items: InputSectionItem[];
  characterCount: number;
};

function itemText(item: InputSectionItem): string {
  if (typeof item.content === "string") return item.content;
  if (Array.isArray(item.content)) {
    const textBlocks = item.content.flatMap((block) => {
      if (typeof block === "string") return [block];
      if (!block || typeof block !== "object" || Array.isArray(block)) return [];
      const record = block as Record<string, unknown>;
      if (typeof record.text === "string") return [record.text];
      if (typeof record.content === "string") return [record.content];
      return [];
    });
    if (textBlocks.length > 0) return textBlocks.join("\n");
  }
  return JSON.stringify(item.content ?? null);
}

export function projectInputSections(payload: unknown): InputSection[] {
  const entries = Array.isArray(payload) ? payload : [payload];
  return entries.map((entry, index) => {
    const record = entry && typeof entry === "object" && !Array.isArray(entry)
      ? entry as Record<string, unknown>
      : null;
    const message = record && Object.hasOwn(record, "message") ? record.message : entry;
    const label = record && typeof record.name === "string"
      ? record.name
      : `messages[${index}]`;
    let item: InputSectionItem;
    if (!message || typeof message !== "object" || Array.isArray(message)) {
      item = { role: null, content: message };
    } else {
      const messageRecord = message as Record<string, unknown>;
      item = {
        role: typeof messageRecord.role === "string" ? messageRecord.role : null,
        // Keep the complete wire message: content, tool_calls, tool_call_id,
        // attachment metadata, and any future provider fields.
        content: message,
      };
    }
    return {
      label,
      items: [item],
      characterCount: itemText(item).length,
    };
  });
}

export function inputSectionFoldKey(inputFoldKey: string, section: InputSection, index: number): string {
  return `${inputFoldKey}:section:${index}:${section.label}`;
}

function requestEntries(record: Record<string, unknown>): Array<{ name: string; message: unknown }> {
  const entries: Array<{ name: string; message: unknown }> = [];
  if (Array.isArray(record.tool_catalog)) {
    entries.push({ name: "tools", message: record.tool_catalog });
  }
  if (Object.hasOwn(record, "tool_choice")) {
    entries.push({ name: "tool_choice", message: record.tool_choice });
  }
  const messages = Array.isArray(record.messages) ? record.messages : [];
  const sections = Array.isArray(record.message_sections) ? record.message_sections : [];
  for (const [index, message] of messages.entries()) {
    const section = sections[index];
    const sectionRecord = section && typeof section === "object" && !Array.isArray(section)
      ? section as Record<string, unknown>
      : {};
    const messageIndex = typeof sectionRecord.message_index === "number"
      ? sectionRecord.message_index
      : index;
    entries.push({
      name: typeof sectionRecord.name === "string"
        ? sectionRecord.name
        : `messages[${messageIndex}]`,
      message: messages[messageIndex] ?? message,
    });
  }
  return entries;
}

export function modelInputSummary(payload: unknown): string {
  const sections = projectInputSections(payload);
  const tools = sections.find((section) => section.label === "tools")?.items[0]?.content;
  const toolChoice = sections.find((section) => section.label === "tool_choice")?.items[0]?.content;
  const messageCount = sections.filter(
    (section) => section.label !== "tools" && section.label !== "tool_choice",
  ).length;
  const imageCount = sections.reduce((total, section) => total + section.items.reduce(
    (sectionTotal, item) => {
      if (!item.content || typeof item.content !== "object" || Array.isArray(item.content)) {
        return sectionTotal;
      }
      const blocks = (item.content as Record<string, unknown>).content;
      if (!Array.isArray(blocks)) return sectionTotal;
      return sectionTotal + blocks.filter((block) => (
        !!block && typeof block === "object" && !Array.isArray(block)
        && (block as Record<string, unknown>).type === "image_url"
      )).length;
    }, 0
  ), 0);
  const parts = [`${messageCount} ${messageCount === 1 ? "message" : "messages"}`];
  if (imageCount > 0) parts.push(`${imageCount} ${imageCount === 1 ? "image" : "images"} sent`);
  if (Array.isArray(tools)) parts.push(`${tools.length} ${tools.length === 1 ? "tool" : "tools"}`);
  if (toolChoice !== undefined) {
    parts.push(`choice ${typeof toolChoice === "string" ? toolChoice : "configured"}`);
  }
  return parts.join(" · ");
}

export function visibleArtifactPayload(raw: string, view: ArtifactView): unknown {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return raw;
  }
  if (view === "raw" || !payload || typeof payload !== "object") return payload;
  const record = payload as Record<string, unknown>;
  if (view === "request") {
    const entries = requestEntries(record);
    if (entries.length > 0) return entries;
    const rounds = Array.isArray(record.rounds) ? record.rounds : [];
    const firstRound = rounds[0];
    if (firstRound && typeof firstRound === "object") {
      const roundEntries = requestEntries(firstRound as Record<string, unknown>);
      if (roundEntries.length > 0) return roundEntries;
    }
    return payload;
  }
  const blocks = Array.isArray(record.content_blocks) ? record.content_blocks : [];
  const toolCalls = Array.isArray(record.tool_calls) ? record.tool_calls : [];
  if (blocks.length > 0 && toolCalls.length > 0) {
    return { content: blocks, tool_calls: toolCalls };
  }
  if (blocks.length > 0) return blocks;
  if (record.content) return record.content;
  return toolCalls.length > 0 ? toolCalls : payload;
}

export type AgentCallRow =
  | { kind: "model"; order: number; round: AgentLlmRound }
  | { kind: "tool"; order: number; call: AgentToolCall };

export function cacheSummary(round: AgentLlmRound): {
  available: boolean;
  input: number | null;
  cachedRead: number | null;
  cacheWrite: number | null;
  output: number | null;
  reasoning: number | null;
  hitRatio: number | null;
} {
  const usage = round.usage ?? {};
  if (usage.cached_read_tokens == null) {
    return {
      available: false, input: usage.input_tokens ?? null, cachedRead: null,
      cacheWrite: null, output: usage.output_tokens ?? null,
      reasoning: usage.reasoning_tokens ?? null, hitRatio: null,
    };
  }
  const input = usage.input_tokens ?? 0;
  return {
    available: true, input, cachedRead: usage.cached_read_tokens,
    cacheWrite: usage.cache_write_tokens ?? null, output: usage.output_tokens ?? null,
    reasoning: usage.reasoning_tokens ?? null,
    hitRatio: input > 0 ? usage.cached_read_tokens / input : null,
  };
}

export function buildAgentCallRows(
  rounds: AgentLlmRound[] = [],
  calls: AgentToolCall[] = [],
): AgentCallRow[] {
  const rows: AgentCallRow[] = [];
  for (const [roundIndex, round] of rounds.entries()) {
    rows.push({ kind: "model", order: roundIndex * 1000, round });
    for (const call of calls
      .filter((candidate) => (
        candidate.invocation_id === round.invocation_id
        && candidate.llm_round_order === round.order
      ))
      .sort((a, b) => a.order - b.order)) {
      rows.push({ kind: "tool", order: roundIndex * 1000 + call.order, call });
    }
  }
  return rows;
}

export function upsertToolCall(
  calls: AgentToolCall[] = [],
  next: AgentToolCall,
): AgentToolCall[] {
  const output = [...calls];
  const index = output.findIndex(
    (call) => call.call_id === next.call_id && call.invocation_id === next.invocation_id,
  );
  if (index >= 0) output[index] = next; else output.push(next);
  return output;
}

export function upsertLlmRound(
  rounds: AgentLlmRound[] = [],
  next: AgentLlmRound,
): AgentLlmRound[] {
  const output = [...rounds];
  const index = output.findIndex((round) => round.round_id === next.round_id);
  if (index >= 0) output[index] = next; else output.push(next);
  return output;
}

export function upsertLlmStream(
  rounds: AgentLlmRound[] = [],
  next: AgentLlmStream,
): AgentLlmRound[] {
  const index = rounds.findIndex((round) => round.round_id === next.round_id);
  const current = index >= 0 ? rounds[index] : undefined;
  // A persisted/completed round is authoritative over delayed SSE snapshots.
  if (current && current.stream_status == null && current.stop_reason !== "pending") return rounds;
  if (current) {
    const attempt = current.stream_attempt ?? 0;
    const sequence = current.stream_sequence ?? -1;
    if (next.attempt < attempt) return rounds;
    if (
      next.attempt === attempt
      && next.status !== "failed"
      && next.sequence <= sequence
    ) return rounds;
  }
  const live: AgentLlmRound = {
    ...current,
    round_id: next.round_id,
    order: next.order,
    role: next.role,
    invocation_id: next.invocation_id,
    model: next.model,
    stop_reason: next.status === "failed" ? "error" : "streaming",
    latency_ms: 0,
    usage: {},
    message_count: current?.message_count ?? 0,
    image_count: current?.image_count ?? 0,
    stable_prefix_hash: current?.stable_prefix_hash ?? "",
    tool_catalog_hash: current?.tool_catalog_hash ?? "",
    stream_status: next.status,
    stream_attempt: next.attempt,
    stream_sequence: next.sequence,
    live_output: next.text,
  };
  const output = [...rounds];
  if (index >= 0) output[index] = live; else output.push(live);
  return output;
}

export function conversationFoldOpen(
  folds: Record<string, boolean>,
  key: string,
  defaultOpen = false,
): boolean {
  return Object.prototype.hasOwnProperty.call(folds, key) ? folds[key] : defaultOpen;
}

export function conversationArtifactEnabled(
  open: boolean,
  refId: string | null | undefined,
): boolean {
  return open && !!refId;
}

export function conversationArtifactQueryKey(
  taskId: string,
  refId: string | null | undefined,
): readonly [string, string, string | null | undefined] {
  return ["agent-artifact", taskId, refId] as const;
}

export function collapsedModelInputSummary(round: AgentLlmRound): string {
  const messages = round.message_count;
  const images = round.image_count;
  const parts = [messages == null
    ? "messages —"
    : `${messages} ${messages === 1 ? "message" : "messages"}`];
  if (images != null && images > 0) {
    parts.push(`${images} ${images === 1 ? "image" : "images"} sent`);
  }
  return parts.join(" · ");
}
