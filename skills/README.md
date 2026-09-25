# ClickClick Skills

[中文](README.zh-CN.md) · [README](../README.md)

Skills hold reusable knowledge about operating apps. Users still describe goals in natural language; the agent selects applicable guidance without requiring Skill IDs or a matching prompt format.

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

Planner receives compact cards for every active workflow. Task aliases and exact
foreground identity rank those cards first; they do not hide capabilities when
an instruction omits the App name. For each execute subgoal, Planner may select one target App and up to
two workflow ids. Executor and Reviewer receive that App's core plus only the
selected role-filtered bodies. Planner prefers app names/aliases; runtime resolves
them and validates explicit package IDs before accepting a new plan and binding
skills. Uninstalled guessed packages cannot establish the stage's skill scope.
Foreground identity remains separate observation
evidence, so an unrelated starting App or overlay does not replace the handoff.
The exact observed foreground App also supplies its core, even when the target
differs or was guessed incorrectly. This adds local control guidance without
changing the goal or selecting foreground workflows. Departed foreground cores
are retired from active delivery; matching target/foreground cores are deduplicated.

Skill ids are globally unique. Duplicate active ids fail closed during loading
instead of silently hiding a foreground workflow. Delivery is traced through
the common active-skill metadata: id, version, content hash, scope, activation
source, and rule categories.

### Interface ownership and system compatibility

Every authored skill declares `interface_scope`:

- `generic`: methods independent of a particular interface, shared across apps
  and systems. General permission reasoning belongs here; fixed permission-page
  sequences do not.
- `app`: third-party app-owned behavior, shared across systems by default and
  matched by exact package. Preinstallation does not make Chrome page content or
  a third-party app's own controls system-specific.
- `system`: system apps or system-owned surfaces (permission pages, document/photo
  pickers, share sheets), even when entered from a third-party app. Requires a
  nonempty `device_profiles` list; missing scope never enables these skills.

`device_profiles` still restricts **any** category when a known exception needs
it. App sharing means permission to reuse, not certification of every app version.
Keep known version conditions in the body; runtime does not enforce app versions.
Package identity alone does not transfer a skill to a clone or another package.
Split app-owned procedures and fixed system-surface procedures into separate
skills rather than sharing the whole mixed flow. A general instruction to inspect
whatever dialog is actually visible need not become a system-specific recipe.

Examples: `interface_scope: app` for Retro Music; `interface_scope: system` with
`device_profiles: [androidworld_api33]` for that system's Files workflow;
`interface_scope: generic` for search recovery. DocumentsUI core is currently
limited to its validated API 33 environment; do not infer API 34 compatibility.

Profile IDs come from `shared/app_alias_profiles.json`. Catalog, exact-ID reads,
and role delivery use the same profile filter; unknown devices get only skills
without profile restrictions. Device switches clear prior scoped delivery.
Traces include interface ownership and declared/selected device profiles. Create
and update APIs accept these fields and validate them before writing a file.

For legacy files, known system-component packages default to `system`, other app
packages to `app`, and app-independent skills to `generic`. This is not a package-
prefix or APK-preinstalled classifier. New skills must explicitly identify their
interface owner, especially new system components and cross-app system surfaces.

Executor cannot load or replace Skills. Planner may load an exact generic Skill
id from its index, but workflow selection uses the supplied cards rather than
free-text search. Referenced resources are not auto-loaded.

Stages can select up to four generic/workflow IDs in total (at most two owned
workflows). For multi-screen record lists, select `adaptive-list-traversal`
alongside the applicable App workflow. It owns anchor-based scroll sizing and
coverage accounting; App skills retain ordering, field comparisons and local
controls. Mentioning a generic skill in an App body does not auto-load it.

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
2. SkillLearner (post-task / Console “learn from this task”) → `_pending/` →
   human approve.
