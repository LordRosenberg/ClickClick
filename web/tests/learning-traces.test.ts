import assert from "node:assert/strict";
import test from "node:test";
import { learningCost, transcriptMessages, tracePayload } from "../src/lib/learningTraces.ts";

test("unrecorded tokens stay unknown and cumulative reserved cost remains explicit", () => {
  assert.match(learningCost(null), /unknown/);
  assert.match(learningCost({calls:2, actions:0, seconds:3, provider_usage:{records:[{total_tokens:20}],unknown_responses:1}}), /tokens unknown/);
  assert.match(learningCost({calls:2, actions:1, calls_include_reserved_upper_bounds:true, provider_usage:{records:[{total_tokens:20},{total_tokens:30}],unknown_responses:0}}), /tokens 50.*预留上界/);
});
test("native and legacy transcripts preserve tool pairs and model uncertainty", () => {
  const messages = [{role:"assistant",tool_calls:[{id:"read",function:{name:"read_history"}}]}, {role:"tool",tool_call_id:"read",content:"missing remains unknown"}];
  assert.deepEqual(transcriptMessages({payload:{messages}}), messages);
  assert.deepEqual(transcriptMessages({phase_input:[{role:"system",content:"policy"}],history:messages}), [{role:"system",content:"policy"},...messages]);
  assert.deepEqual(tracePayload({payload:{analysis:{status:"unknown"}}}), {analysis:{status:"unknown"}});
});

test("bounded API cost projection preserves known total and missing provider usage", () => {
  assert.match(learningCost({calls:2,provider_usage:{total_tokens:42,unknown_responses:0}}), /tokens 42/);
  assert.match(learningCost({calls:2,provider_usage:{total_tokens:null,unknown_responses:2}}), /tokens unknown/);
});
