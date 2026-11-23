# Learning cost and contribution boundaries

[Self-learning design](skill-evolution.md) · [中文](skill-learning-policy.zh-CN.md) · [Architecture](architecture.md)

Ordinary tasks finish the user's objective under the existing Planner and Executor. The default post-task collection is limited to an idempotent local `learning_clue`, with no device access, inference or candidate writes. Recording failure leaves the task outcome intact. The legacy `skill_learn=true`, `CLICKCLICK_SKILL_LEARNING_MODE` and `force` flags are inert.

## Local settings and contribution

`GET/PUT /api/learning/preferences` stores separate `local_recording` (default true) and `contribution_enabled` (default false). Disabling recording affects future collection without deleting history. Disabling contribution immediately prevents further exports of existing records.

`GET /api/tasks/{task_id}/learning-contribution` requires current contribution consent. A whitelist reconstructs only random report ID, actual app packages, terminal state, enumerated mechanical clues, action-type counts and execution counts. It excludes instructions, raw failures, notes, trees, images, typed text/coordinates, task IDs, device serials and raw-content hashes. Unknown counts remain null. Reports still disclose app usage and outcome/counts; users preview before downloading.

This is local export, without automatic upload, point settlement or an official cloud receiver. Contribution consent authorizes neither personal exploration nor model calls. The task list offers no automatic post-completion learning option.

## Android workflow version audit

The Android version audit is opt-in. Set `CLICKCLICK_SKILL_VERSION_AUDIT_ENABLED=true` and enable local recording to capture the Android build for the exact workflow revision delivered to Executor while its app is in the foreground. The first observed build records a baseline. A later build change with paired action evidence can start a read-only run of the same Learner after the task ends.

The background run uses the configured learning request and time limits and zero device actions. The Learner can inspect a chronological outline and read original actions, receipts, observations and available historical screenshots over several rounds. Candidate generation and independent review share that budget. The run covers the app and path supported by the recorded evidence. Gaps that require device exploration or fresh execution checks remain open for explicitly authorized research. Build changes and successful fallbacks are clues for diagnosis.

`GET /api/tasks/{task_id}/skill-links` exposes `version_audits`, `version_audit_run`, and pending candidates for the task. Background version audits still use human approval. Approval of a whole-file deletion requires independent evidence covering the entire skill and a check of the exact reviewed base. The original file is archived when approval removes it from the active library.

## Explicit Skills self-learning

Open **Skills self-learning** on a finished task and approve the model and test-device budgets. The Console sends `POST /api/tasks/{task_id}/learn` with this request.

```json
{
  "accept_model_cost": true,
  "allow_device_operations": true,
  "run_regression": true,
  "max_calls": 24,
  "max_actions": 30,
  "max_seconds": 900
}
```

Ranges are 12–64 calls, 1–100 actions and 30–1800 seconds; defaults are 24/30/900. These limits are not a fixed monetary quote. Missing consent rejects before device resolution or background creation. Callers cannot select official `billing_scope`. Provider retries and phase transitions do not reset the job's deadline or allowance.

Configure a dedicated test emulator and separate ADB server with `CLICKCLICK_SKILL_LOOP_FIXTURE_JSON` for paired runs. The worker can use verified recovery evidence linked to the original recording. It selects an independently reviewed App-scoped addition or revision, reuses verified cases when available, and records and verifies a setup script when the required case is missing. Paired trials hold the App build, restored fixture and task conditions fixed. The report binds the exact candidate, baseline library and delivered Skill guidance. After the publication checks, an eligible low-impact change to one App workflow can publish automatically. A verified high-impact change awaits approval. Missing or failing evidence leaves the active version in place. [Skills self-learning design](skill-evolution.md) · [GUI script design](gui-script-design.md)

API callers that omit `run_regression` or send `false` use the legacy manually initiated research path. That path still uses the task-conditioned Learner and independent Reviewer, with device exclusion, cancellation, environment-change records and cleanup. Eligible results enter the local pending queue for approval. Both paths enforce the evidence and version bindings required by each claim.

## Operator-side workflow

Operators can receive user-supplied reports under their own model/service configuration and validate, deduplicate and aggregate them:

```powershell
python -m evaluation.learning_inbox report1.json report2.json --output issues.json
```

Repeated report IDs do not increase counts; conflicts or extra raw fields are rejected. Aggregation ranks observed apps/mechanical signals, fixes status to `needs_operator_reproduction` and performs no inference, device work or publication. Correlation is not a semantic diagnosis. Operators reproduce issues in their own environment before entering the existing AndroidWorld/evaluation learning workflow.

Public skills still require root-cause, transferability, conflict, regression-risk, concision and marginal-value review plus evidence appropriate to the claim. Local source evidence cannot replace measured utility coverage across a frozen library/candidate delta and the required related variants, near misses and normal tasks. Publication remains manual, rechecking body, library, execution-contract and environment bindings. Public transport, authentication and publication push are future service integration; implemented contribution is local export plus operator inbox, not a deployed cloud backend. Historical learning evidence is linked from [self-learning](skill-evolution.md).
