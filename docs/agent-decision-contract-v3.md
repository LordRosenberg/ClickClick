# Agent 决策合同（三角色）

> 本文描述当前 Reviewer → Planner → Executor 三角色的模型侧合同，取代历史上的
> Manager/Executor v1–v3 合同（`status/plan/subgoal_kind`、`window_phase` 等字段已移除）。
> 持久化读模型对旧版本任务与 trace 数据保持兼容解码。

## 当前模型侧合同

**Reviewer（裁判）**——两次不同的调用：

- **Scope（任务启动，不看 UI）**：`submit_reviewer_scope` 写入不可变任务合同
  `TaskContractBody`：`must_happen`（本次必须实际发生、终态无法证明的操作）、
  `final_ui_state`（可由最终页面证明的结果态）、`answer`（需要回答的问题）、
  `disqualifying_clauses`（禁区）。
- **Boundary（边界裁决）**：`submit_reviewer_decision`，字段包括
  `verdict`（`accept / retry / replan / done / blocked`）、
  `accepted_progress`（每条绑定条件 ref 与证据句柄）、
  `remembered_facts`（每条必须携带 `evidence_handles`）、
  `answers`（仅 `done` 时按 requirement ref 写出）、
  `superseded_progress_ids`（显式作废旧进展）。
  所有引用的证据句柄必须出现在本次裁决包内，由 Harness 校验。

**Planner（规划）**：`submit_planner_decision`，`mode` 二选一：
`execute` 下发一个语义子目标合同（`success_conditions` + `disqualifying_clauses`）；
`review` 发起对当前观察即可判定条件的复核。Planner 不验收、不终止、不写答案。

**Executor（执行）**：`submit_executor_step`，`decision` 三选一：
`act`（恰好一个设备动作）、`request_review`（当前证据似已建立子目标）、
`request_replan`（子目标不安全/不可行/与合同冲突）。边界请求不得携带设备动作。

运行时负责：终态工具强制（`tool_choice=required`）、active observation 绑定、
证据引用校验、动作回执与技能遥测。角色终态只能是工具调用；普通文本经一次调用内
纠偏后仍违规即归类 `malformed`。

## Reasoning configuration

Reasoning is configured per entry in `CLICKCLICK_MODELS_JSON`:

```json
{
  "openai/o3": {
    "provider": "openai",
    "reasoning_supported": true,
    "reasoning": {"effort": "high", "summary": "concise"}
  }
}
```

The gateway passes normalized options only to providers declared capable.
An explicit provider summary is stored as diagnostic-only LLM-round data;
private reasoning fields are ignored. Unsupported requests are recorded as
`unsupported`, without fabricated summary content.

## Skill delivery and authoring

三个闭环角色共享同一投递规则：任务启动时冻结 generic / app-core 路由元数据；
运行时按**精确前台 App**投递该 App 的 core + 全部 active workflows，其他 App 正文不加载。
注入正文经过 trusted-source、scope 校验、按角色分节过滤、hash 追踪与去重。

Author rules under `## Constraints`, `## Hints`, `## Fallbacks`, or
`## Anti-patterns`. Constraints require inline `scope:` and `evidence:`;
fallbacks require `trigger:`. Legacy unclassified prose is treated as a hint.
Learner constraint proposals require operator approval.

## Rollout and rollback

1. Validate tool-schema snapshots, mixed-version replay, role projections,
   skill routing, gateway capability mapping, and Console rendering.
2. Enable reasoning per model only after that provider passes the gateway
   capability tests. A model change is independent of the contract rollout.
3. Compare device-task metrics by scenario: terminal validation retries,
   repeated same-purpose attempts, Executor boundary reports, and dynamic-state
   false positives.
4. To roll back reasoning, remove the per-model `reasoning` object or set
   `reasoning_supported=false`. To roll back model-facing contract changes,
   restore the prior registered tool schemas; versioned read models continue
   decoding both old and current traces. Do not rewrite persisted trace history.
