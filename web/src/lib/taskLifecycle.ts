import type { TaskStatus } from "../api/types.ts";

export function taskIsActive(status?: string): boolean {
  return status === "queued" || status === "running" || status === "pausing"
    || status === "recovering" || status === "waiting_for_device" || status === "recovery_blocked";
}

export function taskIsNonterminal(status?: string): boolean {
  return taskIsActive(status) || status === "paused" || status === "waiting_for_input";
}

export function taskCanPause(status?: string, readOnly = false): boolean {
  return !readOnly && (status === "queued" || status === "running");
}

export function taskCanResume(status?: string, readOnly = false, canResumeFailure = false): boolean {
  return !readOnly && (status === "paused" || ((status === "failed" || status === "recovering"
    || status === "waiting_for_device" || status === "recovery_blocked") && canResumeFailure));
}

export const taskStatusLabel: Record<TaskStatus, string> = {
  queued: "排队", running: "运行中", pausing: "正在暂停", paused: "已暂停",
  waiting_for_input: "等待回答",
  recovering: "正在恢复", waiting_for_device: "等待设备", recovery_blocked: "恢复受阻",
  succeeded: "运行时完成", failed: "运行时失败", cancelled: "已取消",
};
