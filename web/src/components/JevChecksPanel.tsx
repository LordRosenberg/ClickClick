import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getArtifactJson, getJevChecks, type JevCheckEntry } from "@/api/client";

type Phase = { phase: string; request_ref?: string; response_ref?: string; error_category?: string };
type Report = {
  mode?: string; latency_ms?: number; provider_calls?: number; input_tokens?: number;
  thresholds?: unknown; reason?: string; status?: string;
  results?: { path: string; text: string; decision: string; validated: boolean; issues?: string[];
    fidelity_probabilities?: number[]; coverage_issues?: string[]; error_category?: string; cached?: boolean }[];
  batches?: { batch_index: number; requests?: Phase[]; raw_response?: unknown;
    completed_phase_response?: unknown; error_category?: string }[];
};
function JsonFold({ label, value, refId, taskId }: {
  label: string; value?: unknown; refId?: string; taskId: string;
}) {
  useLocale();
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ["jev-artifact", taskId, refId],
    queryFn: () => getArtifactJson(refId!, taskId), enabled: open && !!refId, staleTime: Infinity });
  const text = JSON.stringify(refId ? query.data : value, null, 2);
  return <details onToggle={(e) => setOpen(e.currentTarget.open)} className="rounded border border-border p-2">
    <summary className="cursor-pointer text-xs">{label}</summary>
    {open && <>
      <button className="my-1 text-xs text-cyan" disabled={!!refId && !query.data}
        onClick={() => void navigator.clipboard?.writeText(text ?? "")}>{t("复制完整JSON")}</button>
      <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-5">
        {query.isLoading ? t("加载中…") : query.error ? String(query.error) : text}
      </pre>
    </>}
  </details>;
}
function CommittedSummary({ summary }: { summary: string }) {
  useLocale();
  let parsed: Record<string, unknown> | null = null;
  try { parsed = JSON.parse(summary); } catch { /* legacy plain text */ }
  return <div className="space-y-2 rounded border border-border p-2 text-xs">
    <p className="text-cyan">{t("最终提交摘要（修正或回退后）")}</p>
    {parsed ? Object.entries(parsed).filter(([, value]) => Array.isArray(value)).map(([section, value]) =>
      <div key={section}>
        <p className="text-text-mute">{section}</p>
        {(value as {text?: string}[]).map((item, index) => <p className="my-1 whitespace-pre-wrap" key={index}>{item.text}</p>)}
      </div>) : <p className="whitespace-pre-wrap">{summary}</p>}
  </div>;
}
function Check({ entry, taskId, inline = false }: { entry: JevCheckEntry; taskId: string; inline?: boolean }) {
  useLocale();
  const [open, setOpen] = useState(inline);
  const report = useQuery({ queryKey: ["jev-report", taskId, entry.report_ref],
    queryFn: async () => await getArtifactJson(entry.report_ref, taskId) as Report,
    enabled: open, staleTime: Infinity });
  const sidecar = useQuery({ queryKey: ["jev-io", taskId, entry.io_ref],
    queryFn: async () => await getArtifactJson(entry.io_ref!, taskId) as { requests: Phase[] },
    enabled: open && !!entry.io_ref, staleTime: Infinity });
  const data = report.data;
  const requests = (data?.batches ?? []).flatMap((b) => (b.requests ?? []).map((r) => ({
    ...r, phase: t("批次{value0} / {value1}", { value0: b.batch_index + 1, value1: r.phase }),
  })));
  const phases = requests.length ? requests : sidecar.data?.requests ?? [];
  return <details open={open} className="rounded border border-border p-3" onToggle={(e) => {
    if (e.target === e.currentTarget) setOpen(e.currentTarget.open);
  }}>
    <summary className="cursor-pointer text-sm">
      {entry.kind === "compaction" ? t("摘要") : t("降级/回退")} #{entry.key} {t("· 检查")}{entry.check_index + 1}
      {entry.source_steps.length > 0 && t(" · 来源步骤 {value0}", { value0: entry.source_steps.join(", ") })}
    </summary>
    {open && <div className="mt-3 space-y-2">
      {report.isLoading && <p>{t("加载中…")}</p>}
      {report.error && <p className="text-err">{String(report.error)}</p>}
      {data && <>
        <p className="text-xs text-text-mute">{data.mode} · {Math.round(data.latency_ms ?? 0)}ms ·
          {data.provider_calls ?? 0}{t("次请求 ·")}{data.input_tokens ?? 0}{t("输入token")}</p>
        {(data.reason || entry.reason) && <p className="text-xs text-amber">{data.reason || entry.reason}</p>}
        <div className={inline && entry.committed_summary ? "grid gap-3 md:grid-cols-2" : "space-y-2"}>
        <div className="space-y-2">
        {inline && <p className="text-xs text-text-mute">{t("本次候选（检查前）")}</p>}
        {data.results?.map((r, index) => <div key={index} className="rounded bg-bg-2 p-2 text-xs">
          <p className="text-cyan">{r.path} · {r.decision} · {r.validated ? t("已验证") : t("未验证")}{r.cached && t(" · 缓存")}</p>
          <p className="my-1 whitespace-pre-wrap">{r.text}</p>
          <p>{[...(r.issues ?? []), ...(r.coverage_issues ?? []), r.error_category].filter(Boolean).join(", ")}</p>
          {r.fidelity_probabilities && <p>{t("忠实概率：")}{r.fidelity_probabilities.join(", ")}</p>}
        </div>)}
        </div>
        {inline && entry.committed_summary && <CommittedSummary summary={entry.committed_summary} />}
        </div>
        {phases.map((phase, index) => <div key={index} className="space-y-1">
          <p className="text-xs text-text-mute">{phase.phase}{phase.error_category && ` · ${phase.error_category}`}</p>
          {phase.request_ref && <JsonFold label={t("Jev实际输入：state + questions")} taskId={taskId} refId={phase.request_ref} />}
          {phase.response_ref && <JsonFold label={t("Jev原始输出")} taskId={taskId} refId={phase.response_ref} />}
        </div>)}
        {sidecar.error && <p className="text-err">{t("历史输入索引加载失败：")}{String(sidecar.error)}</p>}
        {!phases.length && !sidecar.isLoading && <p className="text-xs text-text-mute">
          {t("未保存实际请求（历史记录、缓存复用或检查未发出）；不以重建证据冒充请求。")}</p>}
        {!phases.length && data.batches?.map((batch) => (batch.raw_response ?? batch.completed_phase_response) &&
          <JsonFold key={batch.batch_index} label={t("批次{value0}：已保存原始输出", { value0: batch.batch_index + 1 })}
            taskId={taskId} value={batch.raw_response ?? batch.completed_phase_response} />)}
        <JsonFold label={t("完整判定报告与阈值")} value={data} taskId={taskId} />
      </>}
    </div>}
  </details>;
}
export function JevTimelineChecks({ taskId, invocationIds, completionKey }: {
  taskId: string; invocationIds: string[]; completionKey: string;
}) {
  useLocale();
  const query = useQuery({ queryKey: ["jev-checks", taskId], queryFn: () => getJevChecks(taskId) });
  // A live tool can finish after the first index read. Refresh on recorded
  // progress, without polling old tasks or matching unrelated device actions.
  useEffect(() => { void query.refetch(); }, [completionKey]);
  const checks = query.data?.checks.filter((entry) =>
    entry.invocation_ids?.some((id) => invocationIds.includes(id))) ?? [];
  if (query.error) return <p className="text-xs text-err">{t("Jev检查索引加载失败：")}{String(query.error)}</p>;
  if (query.isLoading) return <p className="text-xs text-text-mute">{t("加载当前压缩调用的Jev检查…")}</p>;
  if (!checks.length) return null;
  return <section className="space-y-2 rounded border border-amber/40 p-2">
    <p className="text-xs text-amber">{t("Jev 摘要检查 · 当前压缩调用")}</p>
    {checks.map((entry) => <Check key={`${entry.kind}:${entry.key}:${entry.version}:${entry.check_index}`}
      entry={entry} taskId={taskId} inline />)}
  </section>;
}
export function JevChecksPanel({ taskId, running }: { taskId: string; running: boolean }) {
  useLocale();
  const query = useQuery({ queryKey: ["jev-checks", taskId], queryFn: () => getJevChecks(taskId),
    refetchInterval: running ? 3000 : false });
  return <div className="space-y-3">
    <p className="text-xs text-text-mute">{t("每次摘要检查的实际Jev输入、输出及接纳结果。灰区和降级均不表示验证通过。")}</p>
    {query.isLoading && <p>{t("加载中…")}</p>}
    {query.error && <p className="text-err">{String(query.error)}</p>}
    {query.data?.checks.length === 0 && <p className="text-xs text-text-mute">{t("暂无Jev检查记录。")}</p>}
    {query.data?.checks.map((entry) => <Check key={`${entry.kind}:${entry.key}:${entry.version}:${entry.check_index}`}
      entry={entry} taskId={taskId} />)}
  </div>;
}
