# Learning cost and contribution boundaries

[Self-learning design](skill-evolution.md) · [中文](skill-learning-policy.zh-CN.md) · [Architecture](architecture.md)

Ordinary tasks finish the user's objective. Necessary corrections remain ordinary execution; additional post-task retries and comparisons are explicit research. Legacy `skill_learn=true`, `CLICKCLICK_SKILL_LEARNING_MODE` and `force` do not trigger post-task inference. Terminal tasks may record an idempotent local `learning_clue` without device access, inference or pending writes; recording failure does not change the task outcome.

## Local settings and contribution

`GET/PUT /api/learning/preferences` stores separate `local_recording` (default true) and `contribution_enabled` (default false). Disabling recording affects future collection without deleting history. Disabling contribution immediately prevents further exports of existing records.

`GET /api/tasks/{task_id}/learning-contribution` requires current contribution consent. A whitelist reconstructs only random report ID, actual app packages, terminal state, enumerated mechanical clues, action-type counts and execution counts. It excludes instructions, raw failures, notes, trees, images, typed text/coordinates, task IDs, device serials and raw-content hashes. Unknown counts remain null. Reports still disclose app usage and outcome/counts; users preview before downloading.

This is local export, without automatic upload, point settlement or an official cloud receiver. Contribution consent authorizes neither personal exploration nor model calls. The task list offers no automatic post-completion learning option.

## Personal targeted optimization

Task details offers **Optimize my workflow**. Confirm request/action/time limits and explicit consent for configured model cost and device operations. `POST /api/tasks/{task_id}/learn` accepts:

```json
{
  "accept_model_cost": true,
  "allow_device_operations": true,
  "max_calls": 24,
  "max_actions": 30,
  "max_seconds": 900
}
```

Ranges are 12–64 calls, 1–100 actions and 30–1800 seconds; defaults are 24/30/900. These limits are not a fixed monetary quote. Missing consent rejects before device resolution or background creation. Callers cannot select official `billing_scope`. Provider retries and phase transitions do not reset the job's deadline or allowance.

Personal research always uses task-conditioned Learner and independent Reviewer, retaining device exclusion, cancellation, environment-change records and cleanup. Users may stop it. Results enter local pending only, through source-evidence or measured-utility admission as declared. Missing required evidence, six-part review or version binding remains blocked; human consent cannot replace evidence. Formal files are never hot-written. See [independent review](skill-evolution.md#independent-review-local-facts-and-task-benefit-are-separate).

## Operator-side workflow

Operators can receive user-supplied reports under their own model/service configuration and validate, deduplicate and aggregate them:

```powershell
python -m evaluation.learning_inbox report1.json report2.json --output issues.json
```

Repeated report IDs do not increase counts; conflicts or extra raw fields are rejected. Aggregation ranks observed apps/mechanical signals, fixes status to `needs_operator_reproduction` and performs no inference, device work or publication. Correlation is not a semantic diagnosis. Operators reproduce issues in their own environment before entering the existing AndroidWorld/evaluation learning workflow.

Public skills still require root-cause, transferability, conflict, regression-risk, concision and marginal-value review plus evidence appropriate to the claim. Local source evidence cannot replace measured utility coverage across a frozen library/candidate delta and the required related variants, near misses and normal tasks. Publication remains manual, rechecking body, library, execution-contract and environment bindings. Public transport, authentication and publication push are future service integration; implemented contribution is local export plus operator inbox, not a deployed cloud backend. Historical learning evidence is linked from [self-learning](skill-evolution.md#current-capability-and-remaining-limits).
