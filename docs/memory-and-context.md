# Memory and context management

[Architecture and design index](architecture.md#module-design-documents) · [中文](memory-and-context.zh-CN.md)

**Keep important information, read a bounded context, retrieve detail when needed.** ClickClick stores original records, working notes and structured summaries separately, then assembles the context needed for the current decision.

## What the model sees

<!-- design-diagram:memory-assembly -->

![Memory and context: compact, restore and retrieve](assets/design-memory-assembly.svg)

Compaction replaces only older history. Requirements, plan and notes restore independently; summaries cite original sources, while recent steps and fresh views remain available.

[Mermaid source](diagrams/design-memory-assembly.mmd)

<!-- /design-diagram:memory-assembly -->

The original instruction supplies the goal and user constraints. Planner supplies the current stage and plan; runtime supplies budgets and the latest observation. Notes carry information needed later, while summaries connect earlier execution to recent steps. Each has its own owner.

Planner reads task goals, stage and decision memory; Executor combines the current stage, fresh observation, recent complete history and applicable skills; Reviewer independently assesses disputes and evidence. Requirements/user changes, plan, budget, notes and skills enter context independently rather than being copied through summaries. Summaries cover earlier execution; retrieval supplies detail.

## Notes: carry information forward

Executor writes a note when information is **needed later, may leave context, and is not already adequately retained**. An address needed in another app qualifies; routine clicks, recovered errors and unchanged failed attempts remain in the original history.

A method failure still affecting unfinished work retains its scope and observed outcome. Returning to a usable screen does not erase that obstacle, and retaining it does not create a permanent retry prohibition.

| Content | Purpose | Update behavior |
| --- | --- | --- |
| Body | Complete data, order, sources and necessary uncertainty | Update the same `note_key` for the same subject; earlier versions remain |
| `retained` | Short content directly visible in later decisions | Nonempty text sets it; omission or `null` preserves it; an empty string clears it |
| `unresolved` | A material question still affecting the task outcome | Omission does not clear it; resolution needs an explanation and sources |

`retained` is maintained separately from the body: a body correction may also require updating or clearing its excerpt. Plans own ordinary pending steps; optional additional verification does not automatically become an unresolved question.

| Write operation | Versions and order |
| --- | --- |
| Revise a key | Create a version and keep earlier versions |
| Identical replacement | No new version or refreshed timestamp |
| Append in sequence | Preserve occurrence order, including repeated equal values |

## Compaction: summarize earlier work, keep recent steps intact

A separate compaction session uses the same configured model as Executor and exposes only a summary submission tool. Images are omitted from text compaction; original dialogue, screenshots and note versions remain retrievable.

| Summary section | Keep | Exclude |
| --- | --- | --- |
| `results` | Observed outcomes, artifacts and necessary data | Reports or candidates presented as verified facts |
| `decisions_and_attempts` | Consequential actual attempts, outcomes and scope | Inferred causes, new verification obligations or retry bans |
| `critical_context` | Necessary historical detail absent from the other sections | Copies of the task checklist, current plan or routine action log |

Each item links to its sources. Compaction uses short labels that runtime resolves to durable references. The execution model normally reads concise prose and uses `summary_source` to retrieve the summary and underlying records when details matter.

**Deduplication affects display only.** A summary item is hidden when both its note version and full retained text match a note supplied in the current context. If that note is omitted later, the summary copy reappears. Stored content remains intact; semantic similarity does not authorize merging.

| Compaction rule | Behavior |
| --- | --- |
| Trigger | History exceeds its budget and replacement is expected to release space |
| History budget | 16,000 estimated tokens by default; history only, not the total model window |
| Recent context | Keep the latest two complete execution steps and receipts verbatim |
| Summary capacity | Target 3,000 characters; hard cap 4,000, including headings and separators |
| Invalid submission | At most three model rounds including the initial call, or two in Jev enforce; do not replace active history before validation or forcibly truncate content |

`CLICKCLICK_CHATGPT_HISTORY_TOKENS` configures the ChatGPT history budget; explicit model/role budgets take precedence. A larger budget retains more original text and increases per-request context cost.

### Main configuration

| Setting | Default / purpose |
| --- | --- |
| `CLICKCLICK_EXECUTOR_CONTEXT_TOKENS` | `16000`, Executor history-compaction threshold |
| `CLICKCLICK_CHATGPT_HISTORY_TOKENS` | Optional ChatGPT-channel history budget |
| Model `context.executor.history_tokens` | Explicit model/role history override |
| Model `context.max_input_tokens` | Optional estimated complete-request capacity, separate from history budget |

The complete request also includes prompts, stage, memory, tools and images. Thresholds/image estimates are not provider-billed tokens; compare caching, compaction and extra retrieval together. Recent-history, note and summary capacities follow the protocol in [summary](../agent/revisable/summary.py) and [TaskStore](../agent/revisable/store.py).

<a id="jev-text-fidelity-isolated-experiment"></a>

## Storage: database, files and runtime memory

SQLite retains current state, versioned records and indexes; files retain dialogue, images and large bodies; process memory holds active working copies. See [Session and storage](session-storage.md) for ownership, integrity and backup.

### Finding a note and its earlier versions

**A key names a note; a source reference identifies a particular version of it.** Consider a delivery address the model needs to remember:

| What happens | Stored result | Source reference for retrieval |
| --- | --- | --- |
| Record “8 Pine Road” | Name the note `delivery_address` and save version 1 | `note:delivery_address@1` |
| Correct the address to “18 Pine Road” | Keep the same name and save version 2; version 1 remains | `note:delivery_address@2` |
| Check the originally recorded address | Read version 1 to retrieve “8 Pine Road” | `note:delivery_address@1` |

In the reference, `note` identifies the record type, `delivery_address` is its name, and `@1` selects version 1. The model uses the name to update the note and a versioned reference to retrieve exact saved content.

The database also records the owning task, so two tasks can each have a `delivery_address` note without mixing them. The model's `note_key` is stored as `record_key`; it is a logical name, not an automatically generated row number.

A dialogue file reference such as `dialogue/<hash>.json` is instead a disk path. The database uses it to locate an original message file under `artifacts/`; file paths and versioned note references locate different kinds of stored content.

## Optional Jev text-fidelity checks

Default `CLICKCLICK_JEV_MODE=off`; optional `shadow` records judgments and `enforce` participates in summary admission. Jev checks fidelity to original text, not image pixels or business truth. Inherited citations resolve to original records; old summaries cannot prove themselves.

### Integration point and source evidence

Checking occurs after the compaction model submits a candidate and before Harness accepts it to replace active history. Runtime resolves each item's durable references and supplies the assertion with complete original text. For example, an intention to send email, or successful dispatch with unknown effect, cannot support a summary asserting that the email was sent.

Sources include observation text/UI structure, attributed model reports, tool calls/results and cited note versions, plans, events and measurements. Requirements establish intended work, not completed outcomes. A model report establishes that report rather than becoming an independent observation. Full text retains pronouns, negation, attribution and scope. Oversized evidence is explicitly unverified, never truncated into a purported pass.

### Probability outputs and their roles

The current `facets` path checks whole/contiguous assertions before relation/error diagnostics and applicable detail questions.

| Output | Question and score meaning | Current role |
| --- | --- | --- |
| Fidelity Noul | Probability score for a Boolean question: is the whole assertion, component or explicit target detail supported? Denote it `p_faithful`, with error score `1 - p_faithful` | Primary input to admission and correction |
| Relation Choice | Relation probabilities and confidence for `supports / contradicts / says_nothing` | Diagnostic support, contradiction or lack of evidence; does not independently trigger correction in the current facets policy |
| Error-type Noul | Separate questions for unsupported facts, expanded scope, changed attribution, upgraded certainty and attempts presented as success. Higher scores indicate errors, opposite to fidelity Noul | Explains failures and correction feedback. Types may overlap; a high type score alone does not override fidelity decisions |

Relation classes are exclusive; multiple error types may coexist. Scores retain their question-specific meanings rather than being multiplied as statistically independent events. Target extraction currently covers limited member-selection, English/ISO absolute-date and confirmed-effect expressions. It does not provide a dedicated detail question for every count, date or Chinese expression; other assertions remain covered by whole/component checks.

### How scores decide admission or correction

Harness applies the following priority in `SummaryVerifier.policy()` for each item; Jev does not rewrite the summary:

1. **Check coverage and availability.** Missing sources/answers/required detail scores and service/capacity failures are uncheckable outcomes, handled by the degradation policy below rather than recorded as semantic validation.
2. **Check correction conditions.** Any component error score at least 0.88, whole-score error at least 0.95 for a multi-component item, or applicable targeted-detail error at least 0.74 triggers `repair`. A single assertion uses the component threshold. Multiple failures combine their spans. A narrowly defined contradictory integer sum can also trigger correction; this mechanical check does not replace general factual or mathematical verification.
3. **Check validated support if no correction fired.** All whole/component fidelity scores, including any detail score, must reach 0.60 for `pass / validated=true`.
4. **Otherwise retain a semantic gray result.** Complete checks that meet neither correction nor all-support conditions become `uncertain_bypass / validated=false`; they may be admitted without another correction call. An erroneous gray admission still counts as a miss in evaluation.

A failing component cannot be canceled by other high support scores. Choice confidence and error-type scores explain issues rather than contributing an average or an independent veto. Legacy `relation` confidence/type thresholds and historical isolated long-source conjunction rules belong to different strategies, not the current facets decision rule.

### Threshold selection and freezing

Calibration selects **application decision rules**, without fitting scores to empirical correctness probabilities. Development search and specific error corrections jointly produced the thresholds; one joint grid search did not optimize all four.

- **Samples and labels.** Development records combine historical summaries with complete bound sources and controlled errors. Labels/reasons are checked against source text and withheld from Jev. Once inspected for strategy changes, a held-out set becomes development regression. Historical labels were agent-reviewed without independent human adjudication; corrections preserve original reports.
- **Component threshold 0.88.** The historical search enumerates 0.50 through 0.98 in 0.02 steps, plus 0.99, comparing whole/component aggregation. It first maximizes the minimum of precision, recall and accuracy, then F1, accuracy and other criteria; ties prefer proximity to a predeclared 0.90 reference. Under the contemporary labels, 128 scorable calibration items yielded 15 detections, zero false corrections and one miss at both 0.86 and 0.88; 0.90 missed four. The tie rule selected 0.88, retained for current components rather than established as uniquely optimal.
- **Multi-component whole threshold 0.95.** A correct date-interval assertion received a 0.91 whole-error score without a component reaching rejection. A higher whole threshold conservatively avoids weak whole-level rejection overriding component evidence; this is an error-driven choice, not proof of a global optimum.
- **Target-detail threshold 0.74.** Among 17 member/date development items, the largest correct-item error score was 0.68 and the smallest incorrect-item score was 0.79. Their midpoint, 0.735, became 0.74. That separation describes only those development items.
- **Support threshold 0.60.** Early relation experiments had a minimum correct-item Choice confidence of 0.63: grid value 0.60 covered those items, while 0.65 made some gray. The current policy retains that support threshold without claiming a fresh joint optimization of all present Noul judgments.
- **Post-freeze acceptance.** Freeze labels, sources and thresholds before subsequent requests; tuning on their outcomes would invalidate an independent-test claim. In the 20-item parameter-frozen acceptance, all four controlled errors triggered correction and none of sixteen correct items was falsely corrected, but four correct items remained unvalidated gray. Mechanical capacity/cache fixes occurred during acceptance, so this was not a strict blind test of fully frozen code.

Report cohorts separately: the historical facets development pool has 170 items, with subsequent opened 32- and 26-item sets used for development/regression. Core parameter-frozen acceptance has 20 items. The later 44-item long-source combination is known regression and includes earlier items. A separate early relation stage has 248 opened development items with different provenance and policy. These rounds do not sum to a single 250-item independent real-trajectory benchmark, and repeated checks do not enlarge independent sample size.

Grid-search scripts, quality metrics, phase-specific calibration and final acceptance records remain in the development repository; this section documents the current decision rules, threshold rationale and sample limitations. Limited regression scores establish neither production reliability nor task-success gains from summary detection alone.

### Correction, degradation and reuse boundaries

Feedback supplies rejected spans, sources and reasons. Format and semantic issues share one correction; accepted items retain text and sources. Failed correction keeps accepted/unassessed items from the last mechanically valid candidate, omits known rejections and retains an audit. This can produce incomplete or empty memory while originals and recent steps survive.

Identical complete evidence sets batch together, with at most two concurrent batches sharing an eight-second checking deadline. Default `CLICKCLICK_JEV_FAILURE_POLICY=bypass` admits mechanically valid but unvalidated candidates on service, capacity or coverage failures. Service errors open a provider/credential-scoped 60-second cooldown without spending a correction. Compaction has a 90-second deadline when a candidate exists. No mechanically valid candidate, primary-model authorization/quota, storage failures and cancellation retain their established handling. Experimental `rollback` can still exceed raw-context capacity.

Complete judgments cache by task, assertion, original evidence/context and model/question identity; thresholds are reapplied and faults never cache as passes. Source caps and profile settings are in [Settings](../shared/config.py); calibration and natural/controlled-error results remain in [historical experiments](memory-context-experiments.md). This optional check does not guarantee error-free summaries.

Implementation: [probability questions and error types](../agent/revisable/summary_check.py), [component/detail questions](../agent/revisable/jev_facets.py), [compaction admission and correction](../agent/revisable/summary.py), [Jev gateway](../shared/jev_gateway.py).

## Retrieval: read the source when detail matters

| Need | Read path |
| --- | --- |
| A note outside directly retained content | Obtain its source handle from the note directory and use `read_history` |
| Full note and linked sources | `read_history(source=…, full=true)` |
| Basis for a summary | Read `summary_source`, then follow item sources to dialogue or observations |
| An earlier screen | Read an image by observation source; historical images are not current action coordinates |
| An unknown source | Literal keyword search; no match is not proof of absence |

Context assembly selects whole items within its capacity and reports omissions. Known sources remain directly readable; truncated results include continuation handles. Internal tracing IDs stay in storage, while routine prose exposes usable read/update handles.

### Capacity reference

Text lengths are measured in characters, not tokens; item counts are stated separately.

| Content | Capacity |
| --- | --- |
| Ordinary notes | 64 keys; 6,000 per body |
| Retained excerpts | 600 each; 3,000 for the injected packet including metadata |
| Unresolved questions | Up to 8; 400 each |
| Note directory | 2,400 per page |
| Planner / Reviewer recent notes and operation summary | 12,000 each |
| Measurements | Up to 4; 6,000 budget |
| Ordinary history read / full known-source read | 3,000 / 12,000 |
| Keyword search | Up to 6 matching records |

**Limits:** traceable sources do not prove a conclusion correct. Models can omit information, retain stale judgments or misuse notes; original records support checking and correction. Persistent storage does not guarantee lossless recovery from every interruption.

[Architecture](architecture.md) · [Note and storage implementation](../agent/revisable/store.py) · [Structured summary implementation](../agent/revisable/summary.py)

## Historical experiments

See the [historical record](memory-context-experiments.md) for Jev trials and retired Reader/text-tree/summary-mode experiments rather than current runtime paths.
