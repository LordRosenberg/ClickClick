import type { ProgressEntry } from "@/api/types";

export type PlanRow = {
  text: string;
  kind: "done" | "todo";
  current: boolean;
};

export function buildPlanRows(
  progress: ProgressEntry[],
  plan: string[],
  currentSubgoal: string,
): PlanRow[] {
  const rows: PlanRow[] = [];
  const seen = new Set<string>();
  for (const progressEntry of progress) {
    if (!progressEntry.effective) continue;
    const text = (progressEntry.statement || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    rows.push({
      text,
      kind: "done",
      current: text === currentSubgoal,
    });
  }
  for (const item of plan) {
    const text = (item || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    rows.push({
      text,
      kind: "todo",
      current: text === currentSubgoal,
    });
  }
  return rows;
}
