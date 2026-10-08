import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  cancelTask, getLearningContribution, getLearningPreferences, getLearningDefaults, learnFromTask,
} from "@/api/client";

export function LearningControls({ taskId }: { taskId: string }) {
  useLocale();
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: ["learning-preferences"], queryFn: getLearningPreferences });
  const defaults = useQuery({ queryKey: ["learning-defaults"], queryFn: getLearningDefaults });
  const [acceptCost, setAcceptCost] = useState(false);
  const [allowDevice, setAllowDevice] = useState(false);
  const [calls, setCalls] = useState(24);
  const [actions, setActions] = useState(30);
  const [seconds, setSeconds] = useState(900);
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [budgetEdited, setBudgetEdited] = useState(false);
  useEffect(() => {
    if (defaults.data && !budgetEdited) {
      setCalls(defaults.data.max_calls); setActions(defaults.data.max_actions); setSeconds(defaults.data.max_seconds);
    }
  }, [defaults.data, budgetEdited]);
  useEffect(() => { if (!prefs.data?.contribution_enabled) setPreview(null); }, [prefs.data?.contribution_enabled]);
  const optimize = useMutation({
    mutationKey: ["skill-learning", taskId],
    mutationFn: () => learnFromTask(taskId, { accept_model_cost: acceptCost,
      allow_device_operations: allowDevice, max_calls: calls, max_actions: actions, max_seconds: seconds }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skills"] });
      qc.invalidateQueries({ queryKey: ["learning-traces", taskId] });
      qc.invalidateQueries({ queryKey: ["skill-links", taskId] });
    },
  });
  const stop = useMutation({ mutationFn: () => cancelTask(taskId) });
  const contribution = useMutation({
    mutationFn: () => getLearningContribution(taskId),
    onSuccess: setPreview,
  });
  const validBudget = Number.isInteger(calls) && calls >= 12 && calls <= 64 &&
    Number.isInteger(actions) && actions >= 1 && actions <= 100 &&
    Number.isInteger(seconds) && seconds >= 30 && seconds <= 1800;
  return (
    <details className="rounded border border-border p-3 text-xs space-y-3">
      <summary className="cursor-pointer">{t("Skill 学习与任务统计报告")}</summary>
      <p>{t("正常任务结束不启动探索。公共技能由官方在测试环境学习与验证。")}</p>
      <p>{t("本地任务统计、报告导出开关和学习默认预算可在")}<a href="/setup" className="text-cyan">{t("设置")}</a> {t("中调整。")}</p>
      <p>{t("报告导出默认关闭；报告不包含指令、截图、输入内容或设备标识。可手动下载后用于反馈，不会自动上传。")}</p>
      <Button variant="outline" size="sm" disabled={!prefs.data?.contribution_enabled || contribution.isPending}
        onClick={() => contribution.mutate()}>{t("预览任务统计报告")}</Button>
      {preview && prefs.data?.contribution_enabled && <div>
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap">{JSON.stringify(preview, null, 2)}</pre>
        <Button variant="outline" size="sm" onClick={() => {
          const blob = new Blob([JSON.stringify(preview, null, 2)], { type: "application/json" });
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a"); link.href = url; link.download = "learning-contribution.json";
          link.click(); URL.revokeObjectURL(url);
        }}>{t("下载报告")}</Button>
      </div>}
      <p>{t("Skill 学习（自进化）会使用你配置的模型探索 App 操作方法，并实际操作当前设备。探索结果只进入待评审区；不会自动发布。")}</p>
      <div className="flex flex-wrap gap-3">
        <label>{t("模型请求上限")}<input className="w-16 bg-bg-2" type="number" min={12} max={64} value={calls} onChange={(e) => { setBudgetEdited(true); setCalls(Number(e.target.value)); }} /></label>
        <label>{t("动作上限")}<input className="w-16 bg-bg-2" type="number" min={1} max={100} value={actions} onChange={(e) => { setBudgetEdited(true); setActions(Number(e.target.value)); }} /></label>
        <label>{t("总时限（秒）")}<input className="w-20 bg-bg-2" type="number" min={30} max={1800} value={seconds} onChange={(e) => { setBudgetEdited(true); setSeconds(Number(e.target.value)); }} /></label>
      </div>
      <label className="block"><input type="checkbox" checked={acceptCost} onChange={(e) => setAcceptCost(e.target.checked)} /> {t("接受上述预算内的模型开销")}</label>
      <label className="block"><input type="checkbox" checked={allowDevice} onChange={(e) => setAllowDevice(e.target.checked)} /> {t("允许本次探索操作设备")}</label>
      <Button variant="outline" size="sm" disabled={!acceptCost || !allowDevice || !validBudget || optimize.isPending}
        onClick={() => optimize.mutate()}>{optimize.isPending ? t("正在优化…") : t("优化我的工作流")}</Button>
      {optimize.isPending && <Button variant="outline" size="sm" disabled={stop.isPending} onClick={() => stop.mutate()}>{t("停止本次优化")}</Button>}
      {optimize.data && <p>{optimize.data.ok ? (optimize.data.skipped ? t("本次未发现值得学习的规则。") : t("探索结束；结果以评审与验证为准。")) : optimize.data.reason || optimize.data.error || t("未能启动优化")}</p>}
      {[prefs.error, defaults.error, contribution.error, optimize.error, stop.error].filter(Boolean).map((error, i) =>
        <p key={i} className="text-err">{String(error)}</p>)}
    </details>
  );
}
