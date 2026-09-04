// TypeScript types aligned with the observability-recording timeline contract
// and the SSE event shape emitted by GET /api/tasks/{id}/stream. These mirror
// the Python schemas in shared/schemas.py and the timeline payload from
// control_api/services.ObservabilityQueries.timeline.
//
// Timeline is **step-grouped** (one Step per agent step_seq). Inside each
// step: a shared `observation` selected by role priority
// (executor > reviewer > planner), and optional focused-role members.
// `step_seq` is the unique selection key for the Console — no `Array.find`
// collision because only one Step object exists per `step_seq`.

export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";
export type ObservationMode = "tree-only" | "tree+image" | "image-only";

export type ActionType =
  | "tap"
  | "tap_xy"
  | "type"
  | "replace_text"
  | "swipe"
  | "long_press"
  | "scroll"
  | "drag"
  | "key"
  | "launch"
  | "back"
  | "home"
  | "sleep";

export interface Action {
  type: ActionType;
  index?: number | null;
  x?: number | null;
  y?: number | null;
  x2?: number | null;
  y2?: number | null;
  text?: string | null;
  key?: string | null;
  app?: string | null;
  direction?: "up" | "down" | "left" | "right" | null;
  duration_ms?: number | null;
}

export interface ActionResult {
  success: boolean;
  message: string;
  detail?: Record<string, unknown> | null;
  receipt?: ActionReceipt | null;
}

export interface ActionReceipt {
  transaction_id: string;
  effect_class: "text_input" | "ui_transition" | "gesture" | "none";
  dispatch_succeeded: boolean;
  effect_outcome:
    | "confirmed"
    | "unknown"
    | "timeout"
    | "suppressed"
    | "failed";
  effect_reason?: string;
  deadline_ms: number;
  observation_capture_count: number;
  effect_observation_id?: string;
  observation_id?: string;
  observation_accepted: boolean;
}

export interface ActionPipelineStage {
  stage: string;
  action: Action;
  coordinate_space: "none" | "image" | "original_frame" | "device";
  reason: string;
  observation_id?: string;
  coordinate_space_id?: string;
  transform_id?: string;
  source_geometry?: number[];
  target_geometry?: number[];
  validation_result?: Record<string, unknown>;
}

export interface ActionPipeline {
  origin: "model" | "model_rejected";
  fallback_reason?: string | null;
  missing_required_fields?: string[];
  validation_attempts?: number;
  original_frame_size?: number[];
  compressed_frame_size?: number[];
  basis_observation_id?: string;
  active_observation_id?: string;
  coordinate_space_id?: string;
  transform_id?: string;
  source_geometry?: number[];
  target_geometry?: number[];
  index_set_id?: string;
  validation_result?: Record<string, unknown>;
  stages: ActionPipelineStage[];
  driver_coordinates?: Record<string, number>;
  driver_success?: boolean | null;
  dispatch_suppressed?: boolean;
}

/** Boundary progress entry from TaskMemory (Console plan checklist). */
export interface ProgressEntry {
  progress_id: string;
  statement: string;
  effective: boolean;
  evidence_handles: string[];
  packet_digest: string;
  accepted_step: number;
  source_subgoal: string;
  superseded_by_packet_digest?: string | null;
  superseded_step?: number | null;
}

export interface Task {
  id: string;
  instruction: string;
  status: TaskStatus;
  device_serial?: string | null;
  current_node_id?: string | null;
  current_subgoal?: string;
  plan?: string[];
  progress?: ProgressEntry[];
  step_number?: number;
  failure_reason?: string | null;
  created_at?: number;
  updated_at?: number;
  execution_elapsed_ms?: number;
  read_only?: boolean;
  data_source?: string;
  state?: {
    instruction: string;
    plan: string[];
    current_subgoal: string;
    step_number: number;
    next_role: RoleCallRole;
    role_invocation_count: number;
  } | null;
}

export interface DeviceInfo {
  /** Binding key for create/busy (``lab-a/SERIAL`` on remote hubs; bare serial locally). */
  key: string;
  /** Remote hub id, ``local``, or ``fixture``. */
  driver_id: string;
  /** ADB serial on that host. */
  serial: string;
  state: string;
  model: string;
  market_name: string;
  busy: boolean;
  busy_task_id?: string | null;
}

export interface FailedTask {
  id: string;
  instruction: string;
  failure_reason?: string | null;
  created_at?: number;
  updated_at?: number;
  execution_elapsed_ms?: number;
  current_node_id?: string | null;
  current_subgoal?: string;
  device_serial?: string | null;
  step_number?: number;
  read_only?: boolean;
  data_source?: string;
}

export interface TraceEvent {
  task_id: string;
  node_id?: string | null;
  step_seq?: number | null;
  kind:
    | "llm"
    | "ui"
    | "shot"
    | "dag"
    | "skill"
    | "system"
    | "task_scope"
    | "planner_decision"
    | "reviewer_decision"
    | "executor_tick"
    | "loop_tick"
    | "agent_tool_started"
    | "agent_tool_finished"
    | "agent_tool_failed"
    | "agent_llm_round_finished"
    | "agent_input_safety_degraded"
    | "agent_tool_call_recovery"
    | "harness_event";
  level: LogLevel;
  message: string;
  payload_ref?: string | null;
  payload: Record<string, unknown>;
  ts?: number | null;
}

// --- Per-role tick shapes (still used by SSE events and timeline role members) ---

export interface BaseTick {
  kind: string;
  step_seq?: number | null;
  message?: string;
  level?: LogLevel;
  role_elapsed_ms?: number | null;
  active_skills?: ActiveSkillMetadata[];
}

export type AgentToolStatus =
  | "started" | "succeeded" | "failed" | "unavailable"
  | "timeout" | "budget_exhausted" | "invalid_arguments";

export interface AgentUsage {
  input_tokens?: number | null;
  cached_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  total_tokens?: number | null;
  cache_hit?: boolean | null;
}

export interface UsageMetrics {
  round_count?: number;
  input_tokens_total?: number | null;
  output_tokens_total?: number | null;
  llm_latency_ms_total?: number | null;
  cache_read_tokens_total?: number | null;
  cache_read_ratio?: number | null;
}

export interface AgentLlmRound {
  round_id: string;
  order: number;
  role: "planner" | "reviewer" | "executor";
  invocation_id: string;
  request_ref?: string | null;
  response_ref?: string | null;
  stop_reason: string;
  latency_ms: number;
  usage: AgentUsage;
  message_count: number;
  image_count: number;
  stable_prefix_hash: string;
  tool_catalog_hash: string;
  model?: string;
  response_content_count?: number;
  attachment_count?: number;
  reasoning_status?: "not_requested" | "supported" | "unsupported" | string;
  reasoning_effort?: string | null;
  reasoning_summary_preference?: string | null;
  reasoning_summary?: string | null;
}

export interface AgentToolCall {
  call_id: string;
  order: number;
  role: "planner" | "reviewer" | "executor";
  invocation_id: string;
  llm_round_order: number;
  name: string;
  category: "knowledge" | "observation" | "device_discovery" | "terminal";
  status: AgentToolStatus;
  arguments: Record<string, unknown>;
  result_summary?: string;
  result?: Record<string, unknown>;
  /** Full redacted result returned by the local tool implementation. */
  local_result?: Record<string, unknown>;
  /** Exact compact result serialized into the model's tool message. */
  model_result?: Record<string, unknown>;
  attachments?: Array<{
    label: string;
    kind: string;
    mime_type?: string;
    artifact_ref?: string | null;
    timestamp_ms?: number | null;
    actionable_coordinate_reference?: boolean;
  }>;
  started_at_ms?: number;
  elapsed_ms?: number;
  evidence_refs?: string[];
  artifact_refs?: string[];
  provider_status?: string | null;
  error?: string | null;
  observation_transition?: ObservationTransition | null;
}

export interface ObservationTransition {
  from_observation_id: string;
  to_observation_id: string;
  from_mode: ObservationMode;
  to_mode: ObservationMode;
  attachment_refs?: string[];
  next_active_basis?: string;
}

export interface PlannerTick extends BaseTick {
  kind: "planner_decision";
  mode?: "execute" | "review";
  next_subgoal?: string;
  completion_contract?: Record<string, unknown> | null;
  plan?: string[];
  target_requirement_ref?: string;
  review_requirement_ref?: string;
  llm_input_ref?: string | null;
  llm_output_ref?: string | null;
  // Per-step shared observation refs.
  som_ref?: string | null;
  tree_ref?: string | null;
  observation_mode?: ObservationMode | null;
  gap_reasons?: string[];
  estimated_tokens?: number;
  coordinate_actionable?: boolean;
  index_actionable?: boolean;
  // step-timing-display: monotonic-clock LLM round-trip duration for the
  // focused role only (not the step total). See step-timing-display spec.
  llm_elapsed_ms?: number | null;
  tool_calls?: AgentToolCall[];
  agent_rounds?: AgentLlmRound[];
}

export interface ReviewerAcceptedProgress {
  requirement_ref: string;
  statement: string;
  evidence_handles: string[];
}

export interface ReviewerRememberedFact {
  key: string;
  value: string;
  evidence_handles: string[];
}

export interface ReviewerAnswer {
  requirement_ref: string;
  text: string;
  evidence_handles: string[];
}

export interface ReviewerTick extends BaseTick {
  kind: "reviewer_decision" | "task_scope";
  phase?: "task_scope";
  contract?: {
    must_happen?: string[];
    final_ui_state?: string[];
    answer?: string[];
    final_text_to_user?: string[];
    disqualifying_clauses?: string[];
  } | null;
  verdict?: "accept" | "retry" | "replan" | "done" | "blocked";
  accepted_progress?: ReviewerAcceptedProgress[];
  remembered_facts?: ReviewerRememberedFact[];
  answers?: ReviewerAnswer[];
  superseded_progress_ids?: string[];
  reason?: string;
  evidence_handles?: string[];
  packet_digest?: string;
  final_text_to_user?: string;
  llm_input_ref?: string | null;
  llm_output_ref?: string | null;
  som_ref?: string | null;
  tree_ref?: string | null;
  observation_mode?: ObservationMode | null;
  gap_reasons?: string[];
  estimated_tokens?: number;
  coordinate_actionable?: boolean;
  index_actionable?: boolean;
  llm_elapsed_ms?: number | null;
  tool_calls?: AgentToolCall[];
  agent_rounds?: AgentLlmRound[];
}

export interface ExecutorTick extends BaseTick {
  kind: "executor_tick";
  decision?: "act" | "request_review" | "request_replan";
  action?: Action | null;
  submitted_action?: Action | null;
  effective_action?: Action | null;
  dispatched_action?: Action | null;
  action_pipeline?: ActionPipeline | null;
  action_origin?: "model" | "model_rejected";
  recovery_reason?: string | null;
  summary?: string;
  derived_runtime?: {
    basis_observation_id?: string;
    evidence_refs?: string[];
    active_skills?: ActiveSkillMetadata[];
  };
  action_result?: ActionResult;
  llm_input_ref?: string | null;
  llm_output_ref?: string | null;
  som_ref?: string | null;
  tree_ref?: string | null;
  post_observation_id?: string | null;
  post_tree_ref?: string | null;
  post_annotated_ref?: string | null;
  // Exact active subgoal plus explicit action observation/evidence binding.
  basis_observation_id?: string;
  evidence_refs?: string[];
  subgoal_at_tick?: string;
  observation_mode?: ObservationMode | null;
  gap_reasons?: string[];
  estimated_tokens?: number;
  coordinate_actionable?: boolean;
  index_actionable?: boolean;
  // Historical provider-only timing; canonical call timing lives on RoleCall.
  llm_elapsed_ms?: number | null;
  tool_calls?: AgentToolCall[];
  agent_rounds?: AgentLlmRound[];
  interaction_state?: InteractionStateEvidence | null;
  runtime_budget?: RuntimeBudget | null;
}

export interface RuntimeBudget {
  remaining_steps: number;
}

export interface ActiveSkillMetadata {
  skill_id: string;
  version?: string;
  content_hash: string;
  scope?: string;
  activation_source?: string;
  rule_categories?: string[];
}

export interface FocusedElementEvidence {
  index?: number | null;
  identity: string;
  bounds: number[];
  role: string;
  text?: string;
  desc?: string;
  hint?: string;
}

export interface InteractionStateEvidence {
  focused_element?: FocusedElementEvidence | null;
  focused_editable?: FocusedElementEvidence | null;
  keyboard_visible?: boolean | null;
  confidence: number;
  sources: string[];
  age_ms: number;
}

export interface SkillSummary {
  id: string;
  name?: string;
  description?: string;
  version?: string;
  app_name?: string;
  app?: string;
  kind?: "generic" | "app_core" | "workflow" | "candidate";
  capability?: string;
  intent_tags?: string[];
  tags?: string[];
  triggers?: string[];
  source?: string;
  path?: string;
  body?: string;
  frontmatter?: Record<string, unknown>;
}

export interface PendingSkill {
  id: string;
  gist: string;
  target: string;
  status: string;
  source_task_id?: string;
  outcome?: string;
  app?: string;
}

export interface SkillLinks {
  task_id: string;
  skill_ids: string[];
  pending: PendingSkill[];
}

export interface LoopTick extends BaseTick {
  kind: "loop_tick";
  plan?: string[];
  current_subgoal?: string;
  plan_revision?: number;
}

export type Tick = PlannerTick | ReviewerTick | ExecutorTick | LoopTick;

// --- Step-grouped timeline shapes (Step replaces the legacy flat Tick[]) ---

export interface StepObservation {
  som_ref?: string | null;
  tree_ref?: string | null;
  observation_mode?: ObservationMode | null;
  gap_reasons?: string[];
  estimated_tokens?: number;
  observation_id?: string;
  captured_monotonic_ms?: number;
  frame_geometry?: number[];
  coordinate_actionable?: boolean;
  index_actionable?: boolean;
}

export interface Step {
  step_seq: number | null;
  ts?: number;
  /** Earliest persisted wall-clock trace timestamp for this outer step. */
  wall_started_at?: number | null;
  observation?: StepObservation | null;
  observation_role?: RoleCallRole | null;
  reviewer?: ReviewerTick | null;
  planner?: PlannerTick | null;
  executor?: ExecutorTick | null;
  loop?: LoopTick | null;
}

/** One navigable Reviewer, Planner, or Executor role tick. */
export type RoleCallRole = "reviewer" | "planner" | "executor";

export interface RoleCall {
  call_key: string;
  step_seq: number | null;
  role: RoleCallRole;
  phase?: "scope" | "boundary" | "planning" | "execution";
  observation?: StepObservation | null;
  reviewer?: ReviewerTick | null;
  planner?: PlannerTick | null;
  executor?: ExecutorTick | null;
  elapsed_ms?: number | null;
  llm_elapsed_ms?: number | null;
  metrics?: UsageMetrics;
  wall_started_at?: number | null;
}

export interface TaskTimeline {
  task_id: string;
  status: TaskStatus;
  instruction: string;
  device_serial?: string | null;
  plan?: string[];
  current_subgoal?: string;
  next_role?: RoleCallRole | null;
  role_invocation_count?: number | null;
  /** TaskMemory progress for Header plan checklist (scheme A). */
  progress?: ProgressEntry[];
  step_number?: number;
  failure_reason?: string | null;
  created_at?: number;
  updated_at?: number;
  execution_elapsed_ms?: number;
  read_only?: boolean;
  data_source?: string;
  steps: Step[];
  /** Derived role-call rows for Console navigation; always re-expandable from steps. */
  calls?: RoleCall[];
  task_scope?: ReviewerTick | null;
  metrics?: UsageMetrics & {
    runtime_terminal_status?: {
      status: TaskStatus;
      failure_reason?: string | null;
      semantic_true_success: "not_assessed";
    };
    role_calls?: Record<RoleCallRole, number>;
    role_rounds?: Record<RoleCallRole, number>;
    provider_requests?: Record<RoleCallRole, number>;
    device_actions?: number;
    scope_call_count?: number;
    [key: string]: unknown;
  };
}

// Rendered semantic-tree JSON stored under trees/<ref>.json.
export interface TreeRefPayload {
  app_id?: string;
  activity?: string;
  fingerprint?: string;
  filter_tier?: "concise" | "detailed" | string;
  text_for_llm: string;
  elements_count?: number;
}

// --- live-screen-mirror: client-side mirror state (no backend surface) ---
//
// Mirror state is purely React-local — the orchestrator, traces, and step
// payloads are unchanged. The backend only exposes a single WebSocket route
// `/api/device/mirror/stream`; everything below is owned by MirrorPanel.

/** MirrorPanel canvas mode. `"live"` streams H.264 from scrcpy; `"frame"`
 *  freezes on the currently-selected step's SoM image with the action
 *  hit-point overlay. */
export type MirrorMode = "live" | "frame";

/** Connection lifecycle for the live-mode WebSocket. Transitions:
 *  connecting → live → reconnecting → live (or disconnected → manual retry).
 *  `unavailable` is terminal — scrcpy binary missing on the server. */
export type MirrorConnectionState =
  | "connecting"
  | "live"
  | "reconnecting"
  | "disconnected"
  | "unavailable";

// --- model catalog + ChatGPT login ---

export interface ModelCatalogEntry {
  id: string;
  provider: string;
  is_chatgpt: boolean;
  reasoning_supported?: boolean;
}

export interface ModelCatalog {
  models: ModelCatalogEntry[];
  roles: {
    default: string;
    planner: string;
    reviewer: string;
    executor: string;
    skill_learner: string;
  };
}

export interface ChatGPTStatus {
  authenticated: boolean;
  account_id?: string | null;
  expires_at?: number | null;
  auth_file?: string;
  token_dir?: string;
}

export interface ChatGPTLoginStart {
  user_code: string;
  verify_url: string;
  interval_s: number;
  message?: string;
}

export interface ChatGPTLoginPoll {
  status: "pending" | "authenticated" | "error";
  user_code?: string;
  verify_url?: string;
  interval_s?: number;
  account_id?: string | null;
  expires_at?: number | null;
  auth_file?: string;
  error?: string;
}
