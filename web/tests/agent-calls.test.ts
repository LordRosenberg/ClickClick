import assert from "node:assert/strict";
import test from "node:test";

import {
  buildAgentCallRows,
  agentFoldKey,
  cacheSummary,
  inputSectionFoldKey,
  modelInputSummary,
  observationTransitionLabel,
  projectInputSections,
  visibleArtifactPayload,
  upsertLlmRound,
  upsertToolCall,
} from "../src/lib/agentCalls.ts";
import {
  aggregateCallMetrics,
  agentCallHash,
  callKeyForAgentHash,
  deriveCallRows,
  expandRoleCalls,
  resolveRoleCalls,
  upsertLiveRoleEvent,
  upsertRoleCall,
  usageMetrics,
} from "../src/lib/expandRoleCalls.ts";
import type { AgentLlmRound, AgentToolCall, RoleCall, TraceEvent } from "../src/api/types.ts";

function round(order: number, cached: number | null): AgentLlmRound {
  return {
    round_id: `r${order}`, order, role: "executor", invocation_id: "i",
    stop_reason: "tool_calls", latency_ms: 10,
    usage: { input_tokens: 100, cached_read_tokens: cached },
    message_count: 4, image_count: 0, stable_prefix_hash: "p", tool_catalog_hash: "c",
  };
}

function call(status: AgentToolCall["status"]): AgentToolCall {
  return {
    call_id: "c", order: 1, role: "executor", invocation_id: "i",
    llm_round_order: 1, name: "observe_screen", category: "observation",
    status, arguments: {}, attachments: [{
      label: "end", kind: "image", timestamp_ms: 12,
      actionable_coordinate_reference: true,
    }],
  };
}

test("live lifecycle replaces pending with finished and failed", () => {
  const pending = upsertToolCall([], call("started"));
  assert.equal(pending[0].status, "started");
  const finished = upsertToolCall(pending, call("succeeded"));
  assert.equal(finished.length, 1);
  assert.equal(finished[0].status, "succeeded");
  assert.equal(upsertToolCall(pending, call("failed"))[0].status, "failed");
});

test("live updates preserve cross-invocation arrival order", () => {
  const firstInvocationRound2 = { ...round(2, null), invocation_id: "first" };
  const secondInvocationRound1 = { ...round(1, null), invocation_id: "second" };
  assert.deepEqual(
    upsertLlmRound([firstInvocationRound2], secondInvocationRound1).map((item) => item.invocation_id),
    ["first", "second"],
  );
  const firstInvocationCall2 = { ...call("succeeded"), invocation_id: "first", order: 2 };
  const secondInvocationCall1 = { ...call("succeeded"), invocation_id: "second", order: 1 };
  assert.deepEqual(
    upsertToolCall([firstInvocationCall2], secondInvocationCall1).map((item) => item.invocation_id),
    ["first", "second"],
  );
});

test("conversation preserves recorded model order and inserts its matching tool", () => {
  const rows = buildAgentCallRows([round(2, 0), round(1, 20)], [call("succeeded")]);
  assert.deepEqual(rows.map((row) => row.kind), ["model", "model", "tool"]);
});

test("tool calls only attach to the matching invocation and round", () => {
  const firstRound = { ...round(1, null), invocation_id: "first", round_id: "first:1" };
  const secondRound = { ...round(1, null), invocation_id: "second", round_id: "second:1" };
  const firstCall = { ...call("succeeded"), invocation_id: "first", call_id: "first-call" };
  const secondCall = { ...call("succeeded"), invocation_id: "second", call_id: "second-call" };
  const rows = buildAgentCallRows([firstRound, secondRound], [firstCall, secondCall]);
  assert.deepEqual(rows.map((row) => row.kind), ["model", "tool", "model", "tool"]);
  assert.deepEqual(
    rows.filter((row) => row.kind === "tool").map((row) => row.call.call_id),
    ["first-call", "second-call"],
  );
});

test("cache absent differs from explicit zero", () => {
  assert.equal(cacheSummary(round(1, null)).available, false);
  const zero = cacheSummary(round(1, 0));
  assert.equal(zero.available, true);
  assert.equal(zero.hitRatio, 0);
});

test("temporal ending artifact is marked as coordinate reference", () => {
  const attachment = call("succeeded").attachments?.[0];
  assert.equal(attachment?.label, "end");
  assert.equal(attachment?.actionable_coordinate_reference, true);
});

test("sibling blocks and calls have deterministic independent fold keys", () => {
  const first = round(1, null);
  const input = agentFoldKey("task:executor", first, "input");
  const output = agentFoldKey("task:executor", first, "output");
  const siblingCall = agentFoldKey("other:executor", first, "input");
  assert.notEqual(input, output);
  assert.notEqual(input, siblingCall);
  assert.equal(input, agentFoldKey("task:executor", first, "input"));
});

test("observation basis transition is rendered inline as a connector label", () => {
  assert.equal(observationTransitionLabel({
    from_observation_id: "O1", to_observation_id: "O2",
    from_mode: "tree-only", to_mode: "tree+image",
  }), "O1 tree-only → O2 tree+image");
});

test("artifact presentation keeps the complete model request envelope", () => {
  const messages = [{ role: "user", content: "hello" }];
  const messageSections = [{ name: "observation", message_index: 0 }];
  const request = visibleArtifactPayload(JSON.stringify({
    schema_version: 2,
    messages,
    message_sections: messageSections,
    tool_catalog: [{ function: { name: "large_catalog" } }],
    tool_choice: "required",
  }), "request");
  assert.deepEqual(request, [
    { name: "tools", message: [{ function: { name: "large_catalog" } }] },
    { name: "tool_choice", message: "required" },
    { name: "observation", message: messages[0] },
  ]);
  assert.equal(modelInputSummary(request), "1 message · 1 tool · choice required");
  const blocks = [{ type: "text", text: "world" }];
  assert.deepEqual(visibleArtifactPayload(JSON.stringify({
    content_blocks: blocks,
    usage: { input_tokens: 1000 },
  }), "response"), blocks);
  const toolCalls = [{ name: "observe_screen", arguments: "{}" }];
  assert.deepEqual(visibleArtifactPayload(JSON.stringify({
    content_blocks: blocks,
    tool_calls: toolCalls,
  }), "response"), { content: blocks, tool_calls: toolCalls });
});

test("legacy request without tools keeps all available messages", () => {
  const request = visibleArtifactPayload(JSON.stringify({
    messages: [
      { role: "system", content: "rules" },
      { role: "user", content: "input" },
    ],
  }), "request");
  assert.deepEqual(projectInputSections(request).map((section) => section.label), [
    "messages[0]", "messages[1]",
  ]);
  assert.equal(modelInputSummary(request), "2 messages");
});

test("request messages use prompt-construction variable names as fold labels", () => {
  const messages = [
    { role: "system", content: "## 1. Role\nExecutor rules" },
    { role: "user", content: "SKILL INDEX:\nxiaohongshu" },
    { role: "user", content: "[foreground_app_workflow skill:xiaohongshu]\nfull skill" },
    { role: "user", content: "CURRENT SUBGOAL:\nOpen app" },
    { role: "user", content: "APP RESOLUTION MISS:\nmissing" },
    { role: "user", content: "HISTORY:\n(none)" },
    { role: "user", content: "INPUT:\n{\"ui_text\":\"launcher\"}" },
    { role: "assistant", content: "previous response" },
    { role: "user", content: "unrecognized but preserved" },
  ];
  const names = [
    "system",
    "index_text",
    "k_wire_messages[0]",
    "goal",
    "app_resolution_misses",
    "history",
    "observation",
    "assistant_tools",
    "model_result[observe_screen]",
  ];
  const sections = projectInputSections(messages.map((message, index) => ({
    name: names[index], message,
  })));
  assert.deepEqual(sections.map((section) => section.label), [
    ...names,
  ]);
  assert.ok(sections.every((section) => section.items.length === 1));
  assert.deepEqual(sections.flatMap((section) => section.items.map((item) => item.content)), messages);
  assert.notEqual(
    inputSectionFoldKey("call:input", sections[0], 0),
    inputSectionFoldKey("call:input", sections[1], 1),
  );
  assert.deepEqual(
    projectInputSections(messages).map((section) => section.label),
    messages.map((_, index) => `messages[${index}]`),
  );
});

test("agent invocation hash resolves the containing outer role call", () => {
  const reviewerRound = { ...round(1, null), role: "reviewer" as const, invocation_id: "reviewer-inv" };
  const plannerRound = { ...round(1, null), role: "planner" as const, invocation_id: "planner-inv" };
  const executorRound = { ...round(1, null), invocation_id: "executor-inv" };
  const calls: RoleCall[] = [
    {
      call_key: "2:reviewer", step_seq: 2, role: "reviewer",
      reviewer: { agent_rounds: [reviewerRound] } as RoleCall["reviewer"],
    },
    {
      call_key: "2:planner", step_seq: 2, role: "planner",
      planner: { agent_rounds: [plannerRound] } as RoleCall["planner"],
    },
    {
      call_key: "3:executor", step_seq: 3, role: "executor",
      executor: { agent_rounds: [executorRound] } as RoleCall["executor"],
    },
  ];
  assert.equal(agentCallHash(calls[2]), "#agent-call-executor-inv");
  assert.equal(
    callKeyForAgentHash(calls, "#agent-call-executor-inv"),
    "3:executor",
  );
  assert.equal(callKeyForAgentHash(calls, "#agent-call-missing"), null);
});

test("focused role calls expand Reviewer then Planner then Executor", () => {
  const calls = expandRoleCalls([{
    step_seq: 4,
    reviewer: { kind: "reviewer_decision" },
    planner: { kind: "planner_decision" },
    executor: { kind: "executor_tick", skill_ids: [], pending: [] },
  }]);
  assert.deepEqual(calls.map((call) => call.role), ["reviewer", "planner", "executor"]);
  assert.deepEqual(deriveCallRows(calls).map(({ role, label }) => [role, label]), [
    ["R", "01"], ["P", "02"], ["E", "03"],
  ]);
  assert.deepEqual(deriveCallRows(calls).map(({ phase }) => phase), [
    "review", "plan", "act",
  ]);
});

test("scope is labelled before later calls and usage stays exact", () => {
  const scopeRound = {
    ...round(1, null),
    role: "reviewer" as const,
    invocation_id: "scope-inv",
    latency_ms: 12,
    usage: { input_tokens: 100, cached_read_tokens: 80, output_tokens: 7 },
  };
  const calls = expandRoleCalls([{
    step_seq: 0,
    reviewer: {
      kind: "task_scope",
      phase: "task_scope",
      agent_rounds: [scopeRound],
      role_elapsed_ms: 15,
    },
  }]);
  assert.equal(calls[0].phase, "scope");
  assert.equal(calls[0].elapsed_ms, 15);
  assert.deepEqual(usageMetrics([scopeRound]), {
    round_count: 1,
    input_tokens_total: 100,
    output_tokens_total: 7,
    llm_latency_ms_total: 12,
    cache_read_tokens_total: 80,
    cache_read_ratio: 0.8,
  });
  assert.equal(deriveCallRows(calls)[0].phase, "scope");
  assert.equal(aggregateCallMetrics([calls[0], calls[0]]).input_tokens_total, 100);
});

test("SSE role calls preserve repeated same-step invocations and replay identity", () => {
  const first = {
    step_seq: 7,
    reviewer: {
      kind: "reviewer_decision" as const,
      agent_rounds: [{ ...round(1, null), role: "reviewer" as const, invocation_id: "review-1" }],
    },
  };
  const second = {
    step_seq: 7,
    reviewer: {
      kind: "reviewer_decision" as const,
      agent_rounds: [{ ...round(1, null), role: "reviewer" as const, invocation_id: "review-2" }],
    },
  };
  const one = upsertRoleCall([], first, "reviewer", null);
  const two = upsertRoleCall(one, second, "reviewer", null);
  assert.deepEqual(two.map((call) => call.call_key), [
    "7:reviewer:1", "7:reviewer:2",
  ]);
  assert.equal(upsertRoleCall(two, second, "reviewer", null).length, 2);
  assert.equal(resolveRoleCalls([second], two), two);
});

test("live scope tool and terminal round reconcile to one invocation", () => {
  const scopeTool = {
    ...call("started"),
    role: "reviewer" as const,
    invocation_id: "scope-1",
  };
  const pending = upsertRoleCall([], {
    step_seq: 0,
    reviewer: {
      kind: "task_scope" as const,
      phase: "task_scope" as const,
      tool_calls: [scopeTool],
    },
  }, "reviewer", null);
  const scopeRound = {
    ...round(1, 20),
    role: "reviewer" as const,
    invocation_id: "scope-1",
  };
  const completed = upsertRoleCall(pending, {
    step_seq: 0,
    reviewer: {
      kind: "task_scope" as const,
      phase: "task_scope" as const,
      agent_rounds: [scopeRound],
      tool_calls: [scopeTool],
    },
  }, "reviewer", null);
  assert.equal(completed.length, 1);
  assert.equal(completed[0].call_key, pending[0].call_key);
  assert.equal(completed[0].metrics?.round_count, 1);
});

test("live same-role invocations stay separate and own no sibling observation", () => {
  const eventFor = (invocationId: string): TraceEvent => ({
    task_id: "task",
    step_seq: 7,
    kind: "agent_llm_round_finished",
    level: "INFO",
    message: "round",
    payload: {
      ...round(1, 0),
      role: "reviewer",
      invocation_id: invocationId,
      round_id: `${invocationId}:1`,
    },
  });
  const first = upsertLiveRoleEvent([], eventFor("review-1"));
  const second = upsertLiveRoleEvent(first, eventFor("review-2"));
  assert.equal(second.length, 2);
  assert.deepEqual(second.map((item) => item.observation), [null, null]);
  assert.deepEqual(second.map((item) => item.call_key), [
    "7:reviewer:1", "7:reviewer:2",
  ]);
  const terminal = upsertRoleCall(second, {
    step_seq: 7,
    reviewer: {
      kind: "reviewer_decision" as const,
      reason: "terminal replacement",
      agent_rounds: [{
        ...round(1, 0),
        role: "reviewer" as const,
        invocation_id: "review-2",
        round_id: "review-2:1",
      }],
    },
  }, "reviewer", null);
  assert.equal(terminal.length, 2);
  assert.equal(terminal[1].reviewer?.reason, "terminal replacement");
});
