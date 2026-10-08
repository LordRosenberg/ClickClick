import type { TaskStatus } from "../api/types.ts";

export function taskIsActive(status?: string): boolean {
  return status === "queued" || status === "running" || status === "pausing";
}

export function taskIsNonterminal(status?: string): boolean {
  return taskIsActive(status) || status === "paused";
}

export function taskCanPause(status?: string, readOnly = false): boolean {
  return !readOnly && (status === "queued" || status === "running");
}

export function taskCanResume(status?: string, readOnly = false): boolean {
  return !readOnly && status === "paused";
}

export const taskStatusLabel: Record<TaskStatus, string> = {
  queued: "排队", running: "运行中", pausing: "正在暂停", paused: "已暂停",
  succeeded: "运行时完成", failed: "运行时失败", cancelled: "已取消",
};
