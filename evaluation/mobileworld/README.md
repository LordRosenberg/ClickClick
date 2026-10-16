# ClickClick on MobileWorld

ClickClick owns model calls, GUI actions, native UI-tree/screenshot observations
and scoped skills. MobileWorld supplies task initialization, the emulator image,
its official user simulator and independent final-state predicates.

## GUI-only evaluation

The existing pinned GUI-only reproduction entry is documented in
[English](../../docs/mobileworld-reproduction.md) and
[中文](../../docs/mobileworld-reproduction.zh-CN.md). Its frozen source/profile
is independent of the current HITL runtime. Do not substitute results between
protocols or edit the frozen reproduction bundle.

## User-Int. / HITL

`run_single` and `run_full` accept `--enable-user-interaction` for tasks tagged
`agent-user-interaction` without `agent-mcp`. Batch execution requires an explicit
`--cases` manifest. The same flag must agree with every task's official metadata.
With the flag off, interaction and MCP tasks are rejected by the GUI-only runner.

```powershell
python -m evaluation.mobileworld.run_full --enable-user-interaction --cases interaction-cases.json --expected-device-serial "GUEST_RO_SERIALNO" --model chatgpt/gpt-5.6-sol --output-root data/mobileworld/runs/interaction
```

Replace the serial placeholder with the dedicated emulator's actual `ro.serialno`.
Set the backend and ADB target for your isolated environment. The runner verifies
device identity and app/session, clock and asset readiness before model work.
Continuous screen recording is on by default. Never run two device writers or
initialize/tear down a task while its episode is active.

The broker freezes delegated single-question HITL per task. When `ask_user`
suspends a role invocation, `hitl.py` sends the question through official `/step`
using `action_type=ask_user`. It validates the matching answer and resumes the
same invocation. A question consumes a prediction round. Delegated evaluation
never sends questions to a real user's Console.

Durable dispatch receipts prevent automatic replay after an uncertain transport
failure. Simulator and environment faults remain unscored and stop the batch;
valid model failures retain their official scores. Review run-state, results,
recording manifests and receipts before any recovery. Do not replace a valid
failure with a successful retry.

`user_agent_gateway.py` is an authenticated run-scoped OpenAI-compatible transport
for the official simulator's conversation. Configure its model and random key in
private environment settings. Hidden simulator context must never enter GUI-agent
prompts or public artifacts. Restore simulator configuration after the run.

The original44User-Int run passed **34/44 (77.27%)**, using GPT-5.6-Sol high,
Jev off,50prediction rounds and a2,400-second local task deadline. It retained
44continuous recordings,941prediction frames and56matched questions/answers.
See [methodology and limits](../../docs/mobileworld-hitl-results-20261015.md).

`export_hitl_submission.py` validates all44original goals, official numeric
scores and complete recordings before exporting a local submission package.
The public gallery is a separate reviewed export: `devtools/build_mobileworld_hitl.py`
keeps question/answer events in order, omits authentication replies and retains
all failed cases. Raw databases, private paths, keys and model request logs must
not be published.
