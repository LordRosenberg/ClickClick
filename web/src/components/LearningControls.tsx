import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  cancelTaskSkillLoop, getLearningContribution, getLearningPreferences, getLearningDefaults,
  getTaskSkillLoop, learnFromTask,
} from "@/api/client";
import { skillLoopApiError, skillLoopIsActive, skillLoopReasonLabel, skillLoopStatusLabel } from "@/lib/skillLoop";

const origins = {
  reused: "已复用验证过的用例。",
  recorded: "已补录并登记用例。",
  existing: "已验证并登记现有用例。",
};

export function LearningControls({ taskId }: { taskId: string }) {
  useLocale();
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: ["learning-preferences"], queryFn: getLearningPreferences });
  const defaults = useQuery({ queryKey: ["learning-defaults"], queryFn: getLearningDefaults });
  const loopKey = ["skill-loop", taskId];
  const loopQuery = useQuery({
    queryKey: loopKey,
    queryFn: () => getTaskSkillLoop(taskId),
    refetchInterval: query => skillLoopIsActive(query.state.data?.closure) ? 1500 : false,
  });
  const loop = loopQuery.data?.closure;
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
      allow_device_operations: allowDevice, max_calls: calls, max_actions: actions, max_seconds: seconds,
      run_regression: true }),
    onSuccess: result => {
      if (result.closure) qc.setQueryData(loopKey, { closure: result.closure });
      void qc.invalidateQueries({ queryKey: ["learning-traces", taskId] });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: loopKey }),
  });
  const stop = useMutation({
    mutationFn: () => cancelTaskSkillLoop(taskId),
    onSuccess: result => { qc.setQueryData(loopKey, result); },
    onSettled: () => qc.invalidateQueries({ queryKey: loopKey }),
  });
  const contribution = useMutation({
    mutationFn: () => getLearningContribution(taskId),
    onSuccess: setPreview,
  });
  const validBudget = Number.isInteger(calls) && calls >= 12 && calls <= 64 &&
    Number.isInteger(actions) && actions >= 1 && actions <= 100 &&
    Number.isInteger(seconds) && seconds >= 30 && seconds <= 1800;
  const loopStatus = loop?.status;
  const active = skillLoopIsActive(loop);
  const starting = optimize.isPending;
  useEffect(() => {
    if (loopStatus && loopStatus !== "queued" && loopStatus !== "running") {
      void qc.invalidateQueries({ queryKey: ["skills"] });
      void qc.invalidateQueries({ queryKey: ["learning-traces", taskId] });
      void qc.invalidateQueries({ queryKey: ["skill-links", taskId] });
    }
  }, [loopStatus, qc, taskId]);
  const deniedReason = optimize.data?.ok === false && !optimize.data.closure ? optimize.data.reason : null;
  const loopReasonLabel = skillLoopReasonLabel(loop?.reason);
  return (
    <details className="rounded border border-border p-3 text-xs space-y-3">
      <summary className="cursor-pointer">{t("Skills 自学习与任务统计报告")}</summary>
      <p>{t("使用已配置的模型从任务轨迹学习。优先复用已验证的用例，缺少时自动准备初态并录制，然后在专用模拟器上配对回归。符合条件的低影响修订可自动发布。高影响修订进入待审批。")}</p>
      <fieldset className="space-y-2" disabled={starting || active}>
        <div className="flex flex-wrap gap-3">
          <label>{t("模型请求上限")}<input className="ml-1 w-16 bg-bg-2" type="number" min={12} max={64} value={calls} onChange={(e) => { setBudgetEdited(true); setCalls(Number(e.target.value)); }} /></label>
          <label>{t("动作上限")}<input className="ml-1 w-16 bg-bg-2" type="number" min={1} max={100} value={actions} onChange={(e) => { setBudgetEdited(true); setActions(Number(e.target.value)); }} /></label>
          <label>{t("总时限（秒）")}<input className="ml-1 w-20 bg-bg-2" type="number" min={30} max={1800} value={seconds} onChange={(e) => { setBudgetEdited(true); setSeconds(Number(e.target.value)); }} /></label>
        </div>
        <label className="block"><input type="checkbox" checked={acceptCost} onChange={(e) => setAcceptCost(e.target.checked)} /> {t("接受上述预算内的模型开销")}</label>
        <label className="block"><input type="checkbox" checked={allowDevice} onChange={(e) => setAllowDevice(e.target.checked)} /> {t("授权本次运行操作测试设备")}</label>
      </fieldset>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" disabled={!acceptCost || !allowDevice || !validBudget || starting || active}
          onClick={() => optimize.mutate()}>{starting ? t("正在启动学习…") : t("Skills 自学习")}</Button>
        {active && <Button type="button" variant="outline" size="sm" disabled={stop.isPending || loop?.phase === "cancelling"}
          onClick={() => stop.mutate()}>{t("停止本次学习")}</Button>}
      </div>
      {loop && <div role="status" className="space-y-1">
        <p>{t(skillLoopStatusLabel(loop))}</p>
        {loop.case_origin && <p className="text-text-mute">{t(origins[loop.case_origin])}</p>}
        {loopReasonLabel && <p className="text-text-mute">{t(loopReasonLabel)}</p>}
        {loop.reason && !loopReasonLabel && !active && (
          <details className="text-text-mute">
            <summary>{t("查看结束原因")}</summary>
            <p className="break-words font-mono">{loop.reason}</p>
          </details>
        )}
        {loop.status === "awaiting_publication_review" && loop.pending_id && (
          <Link className="text-cyan hover:underline" to={`/skills?id=${encodeURIComponent(loop.pending_id)}&tab=pending`}>
            {t("查看待审批候选")}
          </Link>
        )}
      </div>}
      {deniedReason && <p role="alert" className="text-err">{t(skillLoopReasonLabel(deniedReason) ?? deniedReason)}</p>}
      {loopQuery.isError && <p role="alert" className="text-err">{t("无法读取 Skills 自学习状态。")}</p>}
      {[optimize.error, stop.error].map((error, i) => error && <p key={i} role="alert" className="text-err">{t(skillLoopApiError(error))}</p>)}
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
      {[prefs.error, defaults.error, contribution.error].filter(Boolean).map((error, i) =>
        <p key={i} className="text-err">{String(error)}</p>)}
    </details>
  );
}
