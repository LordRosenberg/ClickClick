import type { SkillLoopState } from "../api/client.ts";

const phases = {
  learning: "正在从任务轨迹学习并审查候选…",
  planning: "正在查找用例或制定准备计划…",
  recording: "正在录制初态准备步骤…",
  replaying: "正在回放核对并登记用例…",
  regression: "正在比较原 Skill 与候选 Skill…",
  publication: "正在核对发布条件…",
  cancelling: "正在停止 Skills 自学习…",
  completed: "学习流程已结束。",
} satisfies Record<NonNullable<SkillLoopState["phase"]>, string>;

const statuses = {
  queued: "Skills 自学习已排队。",
  running: "Skills 自学习正在运行…",
  awaiting_publication_review: "配对回归通过，候选已进入待审批。",
  regression_passed: "配对回归通过，候选仍需满足发布条件。",
  published: "配对回归通过，低影响修订已自动发布。",
  blocked: "本次学习未通过验证。",
  error: "本次 Skills 自学习未能完成。",
  cancelled: "本次 Skills 自学习已停止。",
} satisfies Record<SkillLoopState["status"], string>;

const reasons: Record<string, string> = {
  fixture_not_configured: "请先配置 Skills 回归专用 Android 模拟器和干净快照。",
  fixture_unavailable: "指定的测试模拟器暂不可用。",
  fixture_visible_to_ordinary_tasks: "测试模拟器出现在普通任务设备列表中。请为回归配置独立设备。",
  automatic_case_plan_unavailable: "任务轨迹未能证实回归分支和验收条件。",
  source_fixture_app_build_mismatch: "来源任务与测试模拟器的 App 版本不一致。",
  fixture_app_build_unavailable: "测试模拟器上缺少对应 App 或无法核对版本。",
  learning_already_running: "这项任务已有 Skills 自学习正在运行。",
  source_task_not_terminal: "请等原任务结束后再开始学习。",
  personal_cost_and_device_consent_required: "请确认模型开销，并授权本次运行操作测试设备。",
  no_reviewed_add_or_update: "本次没有通过审查的新增或修订候选。",
  regression_case_uncovered: "本次未能建立可稳定验证的回归用例。",
  case_recording_or_replay_failed: "初态准备脚本未能通过录制或回放验证。",
  paired_regression_not_passed: "配对结果未通过检查，候选暂未进入待审批。",
  tested_skill_delivery_unverified: "无法确认实际执行加载了对应的 Skill 内容。",
  active_skill_library_changed: "评测期间 Skills 库发生变化，请重新运行。",
  existing_skill_admission_not_passed: "候选暂未满足现有发布条件。",
  learning_budget_exhausted: "本次运行用完了学习预算。",
  skill_loop_budget_exhausted: "本次运行用完了学习预算。",
  time_budget: "本次运行用完了学习预算。",
  action_budget: "本次运行用完了学习预算。",
  model_budget: "本次运行用完了学习预算。",
};

export function skillLoopIsActive(loop?: SkillLoopState | null) {
  return loop?.status === "queued" || loop?.status === "running";
}

export function skillLoopStatusLabel(loop: SkillLoopState) {
  return loop.status === "running" && loop.phase
    ? phases[loop.phase] ?? statuses.running
    : statuses[loop.status] ?? statuses.error;
}

export function skillLoopReasonLabel(reason?: string | null) {
  return reason && Object.prototype.hasOwnProperty.call(reasons, reason) ? reasons[reason] : undefined;
}

export function skillLoopApiError(error: Error) {
  const detail = error.message.match(/^\d{3} (\{.*\})$/s);
  if (detail) {
    try {
      const code: unknown = JSON.parse(detail[1]).detail;
      if (typeof code === "string") return skillLoopReasonLabel(code) ?? code;
    } catch { /* Preserve the original HTTP error if the payload is not JSON. */ }
  }
  return skillLoopReasonLabel(error.message) ?? error.message;
}
