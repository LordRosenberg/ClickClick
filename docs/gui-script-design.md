# GUI script design

[中文](gui-script-design.zh-CN.md) · [Architecture](architecture.md#module-design-documents) · [Skills self-learning](skill-evolution.md) · [Commands and examples](../evaluation/gui_scripts.md)

ClickClick can turn a completed Android task into a versioned GUI script. A script captures a repeatable path through an App. Each step specifies where to start, what to do, and what visible result to verify. A single script can use the accessibility tree for one step and screenshot evidence for the next.

```text
Opt-in task → accepted observations and action receipts → Learner draft → independent review
            → clean replay on an isolated emulator → script registry
Replay failure → bounded recovery → recheck and resume → evaluate a local revision or a Skill hint
```

## From a task to a script

1. **Opt in before the task.** The **Record script** option sets `generate_test_script`. The task keeps its normal execution path. The recorder stores each available clean image by observation ID, together with pixel dimensions, coordinate space, rotation and crop metadata. Cases with missing pixels or unaccepted observations carry an explicit reason. Ordinary tasks leave this retention off.
2. **Ground each step in the recorded path.** A successful opted-in task queues a background job. The compiler reads confirmed actions and their linked accepted observations for one Android App. An observed tree action uses semantic selectors. A coordinate tap uses visual object evidence from the clean before and after frames. A recorded launch stays a launch. The Learner proposes selectors, checks and visual regions for the observed action modes and order. The program checks those proposals against the original evidence and rejects gaps or unsupported transforms.
3. **Connect steps across observation modes.** If a tree action opens a screenshot-only screen, the next visual target can verify the transition when its identity is present after the action and verifiably absent before it. The previous step's verified effect also becomes a guard for the next step. The final check must distinguish the completed state from the initial state.
4. **Review and package.** The independent Reviewer checks the path, target identity, result and parameter binding. The download contains `script.json` and the referenced visual assets. Script and asset hashes bind the reviewed content. Generation reads artifacts on a separate budget and uses no device input. A reviewed draft awaits live replay.

The script declares its App, revision, parameters, `initial` and `final` conditions, plus an ordered list of steps. Each step contains `before`, `action` and `after`, with optional `blocked_when`, `retry_when` and `timeout_seconds`. Inputs are declared parameters. Automatic mixed authoring currently stops when a recording combines visual taps and parameterized input, preventing a screenshot template from freezing a demonstrated input value.

## How replay identifies the target

| Evidence available | How the executor binds the action |
| --- | --- |
| Actionable accessibility tree | Resolve semantic fields such as resource ID, role, text and ancestor against the current observation. An explicit relative anchor or a scoped, geometry-checked occurrence can disambiguate repeated controls. Text entry requires the matching focused editable field. |
| Tree with multiple matching tap candidates | When the step supplies visual evidence, constrain the verified image hit to current, actionable tree candidates. A single visual template can distinguish those candidates. An image-only tap requires visual object evidence. |
| Screenshot only | Establish the visual object's identity in the current frame, confirm a unique control in that object, and project the stored relative touch onto the current control bounds. |

### Recording a visual object

For a visual tap, the Learner proposes the bounds of a visible row, card or panel and disjoint rectangles inside it. Identity regions describe the object. A control region contains the demonstrated touch. An outcome region shows the requested change. Boundaries can help separate adjacent objects, and regions labeled dynamic are excluded from the template. For an alarm, the alarm label and set time can identify the row, while a relative date such as “tomorrow” can be excluded.

The program crops from the original image and keeps only the selected regions. It verifies that the recorded object resolves to the actual touch, can be identified on the after frame, and shows the selected outcome only after the action. Additional before frames can check the same initial state. Automated recording uses image regions as identity evidence. A script that explicitly declares a text identity can use local OCR at replay to require a unique exact text match inside the same container.

### Searching the current screen

Visual replay uses full-resolution captures. The default local search region is the recorded object box with a margin on each axis of half its size or 12 pixels, whichever is larger. Recording normally authorizes a full-frame fallback unless a region is explicitly limited. Replay gives local placements priority and checks alternatives across the authorized scope. The matcher tests a coherent move or scale of the recorded regions, and can locate rearranged parts inside a row or card independently inferred from current background blocks or separators.

In each frame, the identity and the control or outcome must share one visible container. A second plausible object, multiple controls in a matched object, or unresolved ownership blocks the tap. The default similarity threshold is `0.92` and the minimum score gap is `0.04`. A second object at or above threshold also creates ambiguity even if the gap is large. Scores are image-matching parameters. Recorded bounds and geometric checks constrain where the match can authorize input. For a verified hit, the runner calculates the touch from the current control box and checks the match again on a fresh frame immediately before dispatch.

## Waiting, execution and handback

Replay checks the initial conditions, polls fresh observations for each step's local preconditions, dispatches through the existing action and observation transaction, then polls for the postcondition. Each step has a precondition deadline and a second deadline shared by dispatch and the postcondition check. They use the same per-step setting, 12 seconds by default or 15.5 seconds for launch. A step can override that setting. An explicit `wait` polls for a condition without sending input. A matching `blocked_when` can end the precondition wait early. After the last step the runner checks the App and final outcome on a fresh observation.

The script runs without a model until a step fails. Limited recovery checks authorized in-App actions, verified locator changes or dismissible App-owned dialogs. If this fails, the normal Executor receives the task, stage, current step, observation, device receipts and completed-step evidence. When the route assumptions fail, Executor sends `replan` through the Harness to Planner. Subsequent takeovers use the existing task time and action budgets.

The runner verifies the failed step on a fresh observation and atomically saves its completion and the next cursor. It ends that takeover and checks the next step's preconditions at the normal step entry point. A failure there gets its own takeover.

| Decision | Required evidence |
| --- | --- |
| Retry this step once | The attempt was demonstrably undispatched, the current preconditions hold and the blocker has cleared. |
| Continue to the next step | The failed step's result has been verified. For steps with a declared confirmation record, a fresh transaction identifier proves the result. |
| Finish | The last step is confirmed and the final condition and any independent result check pass. |
| Pause for confirmation | The current submission remains uncertain. The runner preserves the cursor and requests operator confirmation. |

T1 binds input intent to a script revision, step and attempt. T2 saves the device receipt, and T3 records the executed step. If T1 survives an interruption or the action may have reached the App, the Harness permits read-only verification for that step. A step may declare `confirmation` to identify an App-owned record container and an identifier field. A single new identifier in the same container can prove the submission even when the confirmation page is absent. If the operator has to open the record manually, Resume reads evidence again from the same task and device.

## Verification and version registration

Explicit replay targets a dedicated Android emulator. It checks the serial, AVD identity and installed App build. The script's initial conditions describe the required state. A launch can open the App if it is part of the script. Before each snapshot-backed verification run, the system restores the entire AVD to the named snapshot and checks the App build and initial state. The Console's fixture uses a separate ADB port and must be invisible to the ordinary task pool. If no fixture is configured or required inputs are unavailable, the downloadable draft remains available for explicit verification.

| Path | Evidence required for registration |
| --- | --- |
| New script | Independent review of the exact draft and two clean, unassisted replays from the same initial state. The runner verifies that the final conditions are unmet at the start and satisfied at the end. Passive steps and qualifying semantic tab taps can auto-register after both passes. Other verified scripts await approval. |
| Explicit repair | An assisted success with native evidence can yield a local Learner proposal for one failed step. Keep the script's goal, inputs, final conditions and unrelated steps, and preserve the failed step's expected effect. The Reviewer checks the proposal, and unassisted replay from the same snapshot must verify it before approval. |
| Automatic repair | The revision must also pass without help from the previously registered original snapshot. Automatic registration is restricted to an exact, narrowly scoped selector repair. Other verified changes await approval. |

When a one-time blocker is cleared and the original step runs successfully, the run records the takeover and keeps the existing script version. A confirmed tap that already moved the App before its postcondition timed out is eligible only for a Skill hint. For eligible changes the registry rechecks the reviewed hash, replay receipts and base revision. It stores numbered, immutable revisions so a caller can replay an explicit version.

## How scripts support Skills learning

An assisted run can yield App knowledge, such as a verified shortcut to the same result. The repair Learner can save a Skill hint tied to the native action and observation references, App build and reviewed source script. A hint can be saved independently of proposing a script patch. Locator-only repairs and a verified retry of the original step leave no Skill hint. The subsequent explicit Skills learning run can use qualifying hints as evidence and verify the suggested rule.

For Skill regression, a setup script prepares the test state. The evaluator checks the case's expected branch conditions and confirms that the business goal is still unmet. It then executes an ordinary ClickClick task with the baseline Skills or exact candidate and checks the result and preservation guards independently on each side. The paired run tests the Skill's effect on the Agent's decisions. See [Skills self-learning](skill-evolution.md) and the [evaluation design](evaluation-design.md).

The visual validation includes live input on Google Clock and Android Files and separately recorded screenshot comparisons. The comparisons include lookalikes, cross-card matches and successful reflow, with wrong taps and abstentions recorded separately. A tree → screenshot → tree task was also drafted, downloaded and replayed on a dedicated Android fixture.
