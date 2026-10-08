import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import type { RevisablePlan } from "@/api/types";

export function RevisablePlanView({ runtime }: { runtime: RevisablePlan }) {
  useLocale();
  const plan = runtime.plan;
  if (!plan?.current_stage) return null;
  const reported = runtime.completed_stage_ids.includes(`plan_${runtime.revision}_stage_1`);
  return (
    <div className="space-y-2 text-xs">
      <div className="text-text-mute">{t("计划版本")}{runtime.revision}</div>
      <p className="text-cyan">{reported ? t("已汇报阶段结果") : t("当前目标")}：{plan.current_stage.goal}</p>
      {plan.assumption_roadmap.length > 0 && (
        <div className="text-text-mute">
          <p>{t("后续设想（待验证，将随执行反馈更新）")}</p>
          <ul className="space-y-1">
            {plan.assumption_roadmap.map((goal, index) => <li key={index}>{goal}</li>)}
          </ul>
        </div>
      )}
      {runtime.feedback && <p className="whitespace-pre-wrap text-text">{t("反馈：")}{runtime.feedback}</p>}
    </div>
  );
}
