# ClickClick Skills

[中文](README.zh-CN.md) · [README](../README.md)

Skills hold reusable knowledge about operating apps. Users still describe goals in natural language; the agent selects applicable guidance without requiring Skill IDs or a matching prompt format.

This guide covers authoring and format; runtime rules are owned by [skill delivery](../docs/skill-delivery.md). See [Skill self-improvement](../docs/skill-evolution.md) for extraction from real tasks, independent review and validation. Learning requires an explicit research entry point and does not automatically publish candidates.

<a id="when-to-add-a-skill"></a>
## When should you add a Skill?

Start with the skills already included and run a representative task. Add or revise a Skill when repeated runs expose a stable, explainable gap in app knowledge. Useful cases include:

| Situation | Knowledge to capture |
| --- | --- |
| Non-obvious app behavior | Hidden entry points, editing versus saving, control meanings and applicable app or system versions. |
| Information lost across apps | Fields to preserve, destination field mappings and when to revisit the source. Actual recipes or expenses remain task input. |
| Long lists or similar records | How to distinguish records, retain progress and decide when searching or traversal is complete. Reuse the generic traversal skill where applicable. |
| Partially completed forms | Field dependencies, post-submit checks and evidence that the app saved the result. |
| Frequently repeated app operations | Proven methods, known failure patterns and alternative paths with explicit triggers. |

For example, if runs repeatedly stop after filling a form that requires a separate save action, capture the save behavior and resulting-state check in the app Skill. A particular title, amount or date belongs in the current task, rather than a reusable rule.

A `workflow` Skill is guidance for a class of tasks, not a prompt users must copy or a fixed click script. Describe goals and observable states so the agent can adapt to the current screen. Ordinary guidance should remain advice; hard constraints require a clear scope and evidence.

## How do you validate an improvement?

1. Inspect the failed call's model input, active skills, action receipts and final screen in Console. Establish whether the issue is missing or incorrect operating knowledge. If an existing Skill was not delivered, check its description, app and device scope first.
2. Add the smallest reusable lesson to the app core or an existing task Skill. Prefer revising existing guidance over duplicating it.
3. Validate with different data and representative starting screens, checking the saved application state. Keep model, app versions and budgets fixed for comparisons, and retain failed attempts.

Skills can reduce known errors but cannot guarantee success. Provider timeouts, disconnected devices, stale screenshots and tool implementation errors require fixes to the corresponding components; extra prompt text cannot replace those fixes.

Filesystem skills are the **only** runtime source of truth for agent priors.
There is no SQLite / DB skill store.

## Runtime delivery

Catalog cards → Planner stage selection → device/app scoping → role-specific bodies. [Skill delivery design](../docs/skill-delivery.md) owns limits, foreground core handling and compound-action permissions.

<a id="interface-ownership-and-system-compatibility"></a>

Declare `interface_scope`, provide `device_profiles` for system UI and retain known app-version conditions in prose. Separate app knowledge, workflows and system-interface procedures; exact matching rules are in the design above.

## Layout

```
skills/
  generic/<name>/SKILL.md   # cross-app priors
  apps/<app_id>/core/SKILL.md
  apps/<app_id>/workflows/<workflow-id>/SKILL.md
  apps/<app_id>/workflows/<workflow-id>/references/  # optional, not auto-loaded
  _pending/                 # Learner patches — NOT in the hot index
```

## File format

YAML frontmatter + Markdown body (Hermes / OpenClaw style):

```markdown
---
name: bilibili-search-open-video
description: B站入口、搜索打开视频与常见遮罩。用户提到B站时使用。
version: 0.1.0
app: tv.danmaku.bili
kind: workflow
capability: search_open_video
tags: [video, search]
# App core/workflow only; recipes use the closed validated template vocabulary.
verified_actions:
  - id: example.inspect_and_return
    template: tap_capture_key
    key: back
    purpose: Open the target, preserve detail evidence, and return.
---

# Title

## Procedure
...

## Verification
...

## Pitfalls
...

## Constraints
- Never do X | scope: surface/workflow | evidence: stable source or trace refs

## Hints
- Prefer Y when it is visible

## Fallbacks
- Use Z | trigger: primary path is absent or failed

## Anti-patterns
- Repeating W caused a demonstrated loop

## Executor notes   # optional — Planner/Reviewer do not see this
...

## Decision notes   # optional — Executor does not see this; Planner/Reviewer do
...
```

### Conventions

- Keep `app_core` small and limited to stable app-wide guidance. Put each stable
  user intent in its own `workflow` directory.
- Every workflow declares `app`, `capability`, `description`, and `version`,
  with non-empty `## Procedure` and `## Verification` sections. Do not add
  retrieval triggers or surface inventories; the model selects from the whole
  app catalog.
- `verified_actions` is App-scoped execution authorization, not a discovery
  hint or free-form macro language. Only an `app_core` or workflow may declare
  a recognized closed recipe. Runtime permits its exact id only while that
  Skill is active and its `app` matches the current foreground package; generic
  Skills cannot grant it. Internal recipe fields remain outside model-facing bodies.
- Procedure steps are guidance, not automatically hard constraints.
- Shared body is the default. Use role notes only for facts clearly useful to
  one role (e.g. `need_image` hints for Executor).
- Classify rules conservatively:
  - **Constraint**: a hard scoped invariant; every item requires `scope` and
    `evidence` metadata. Use only when violation is impossible, invalid, or unsafe.
  - **Hint**: advisory success/efficiency guidance. This is the default for
    uncertain prose.
  - **Fallback**: an alternate path with an explicit `trigger`.
  - **Anti-pattern**: a demonstrated loop, failure, or harmful action.
- A current observation may override hints, but not an applicable constraint.
  If a constraint conflicts with the subgoal, Executor returns `replan`.
- Pending Learner output under `_pending/` requires explicit approve before it
  becomes canonical. Review must choose app-core merge, existing-workflow merge,
  new workflow, or inactive candidate. Learner-authored constraints additionally
  require narrow scope, evidence, and explicit operator approval.

## Authoring tracks

1. Hand-written teacher skills for focus apps.
2. Explicit SkillLearner entry (Console “Optimize my workflow”) → `_pending/` →
   human approve.
