# ClickClaw Skills

Filesystem skills are the **only** runtime source of truth for agent priors.
There is no SQLite / DB skill store.

## Runtime delivery

Once an observation establishes the exact foreground package, Reviewer,
Planner, and Executor receive that App's core plus every active workflow body. No other App's
workflow is delivered. Harness does not rank, select, or interpret workflows;
the model chooses applicable guidance from the complete exact-App bundle and
ignores adjacent procedures with unrequested effects.

Skill ids are globally unique. Duplicate active ids fail closed during loading
instead of silently hiding a foreground workflow. Delivery is traced through
the common active-skill metadata: id, version, content hash, scope, activation
source, and rule categories.

Complete exact-App delivery is intentionally simple for the current small
catalogs. Catalog size, prompt tokens, cache reuse, accuracy, and adjacent-rule
interference must be measured on device; any future retrieval layer must first
prove required-workflow recall on a frozen representative set.

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
