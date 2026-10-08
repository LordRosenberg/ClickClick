import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useState } from "react";
import { useIsMutating, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { getArtifactJson, getPendingSkill, getTaskLearningTraces } from "@/api/client";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { learningCost, object, pretty, tracePayload, transcriptMessages } from "@/lib/learningTraces";
import { formatWallTime } from "@/lib/utils";

function JsonFold({ title, value, open = false }: { title: string; value: unknown; open?: boolean }) {
  useLocale();
  if (value == null) return null;
  return <details className="rounded border border-border bg-bg-2 p-2" open={open || undefined}>
    <summary className="cursor-pointer font-mono text-xs text-text-mute">{title}</summary>
    <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5">{pretty(value)}</pre>
  </details>;
}
function Transcript({ value }: { value: unknown }) {
  useLocale();
  const payload = tracePayload(value);
  const messages = transcriptMessages(value);
  const analysis = payload.analysis ?? object(payload.decision).trajectory_analysis;
  const decision = object(payload.decision);
  const { trajectory_analysis: _analysis, ...decisionSummary } = decision;
  return <div className="space-y-2">
    {payload.analysis_current === false && <p className="text-xs text-amber">{t("此分析是保留的检查点，尚未覆盖最新交流。")}</p>}
    {analysis != null && <JsonFold title={t("轨迹分析 · 模型判断")} value={analysis} open />}
    <JsonFold title={t("提案 / 审查结论 · 模型判断")} value={payload.decision == null ? null : decisionSummary} open />
    <JsonFold title={t("主机返回的学习结束结果 · 接收与收益验证分开解释")} value={payload.result} open />
    <JsonFold title={t("引用与实际读取记录")} value={payload.evidence_reads} />
    {messages.length > 0 && <p className="font-mono text-xs text-text-mute">{t("已持久化交流 ·")}{messages.length} {t("条消息 · 图片字节按原记录省略")}</p>}
    {messages.map((message, index) => <div key={index} className="space-y-1">
      <JsonFold title={`${index + 1} · ${String(message.role || "unknown")} ${message.tool_call_id ? `· ${String(message.tool_call_id)}` : ""}`}
        value={message.content} open={payload.decision == null && message.role === "assistant" && index === messages.length - 1} />
      <JsonFold title={t("请求的工具及参数")} value={message.tool_calls} />
    </div>)}
    <JsonFold title={t("完整观测记录")} value={value} />
  </div>;
}
function PendingReview({ id }: { id: string }) {
  useLocale();
  const query = useQuery({ queryKey: ["pending-learning-review", id], queryFn: () => getPendingSkill(id) });
  if (query.isLoading) return <p>loading review…</p>;
  if (query.error) return <p className="text-err">{String(query.error)}</p>;
  const review = object(object(query.data?.meta).review);
  return <div className="space-y-2">
    <p className="text-xs text-text-mute">{t("来自 pending 元数据的独立审查结论。没有保存的模型轮次仍为 unknown。")}</p>
    <JsonFold title={t("Reviewer 独立审查 · 模型判断")} value={review.review} open />
    <JsonFold title={t("候选预审")} value={review.preflight} />
    <JsonFold title={t("验证记录 · 以实际匹配和评测证据为准")} value={review.validation} />
    <JsonFold title={t("适用条件与收益主张 · 模型判断")} value={review.candidate_contract} />
    <p className="font-mono text-xs text-text-mute">{learningCost(review.cost)} {t("· 累计快照")}</p>
    <Link className="text-cyan text-xs hover:underline" to={`/skills?id=${encodeURIComponent(id)}&tab=pending`}>{t("查看候选正文和差异")}</Link>
  </div>;
}
export function SkillLearningPanel({ taskId }: { taskId: string }) {
  useLocale();
  const learningActive = useIsMutating({ mutationKey: ["skill-learning", taskId] }) > 0;
  const [sessionOffset, setSessionOffset] = useState(0);
  const [resultOffset, setResultOffset] = useState(0);
  const [beforeEntry, setBeforeEntry] = useState<number | null>(null);
  const query = useQuery({ queryKey: ["learning-traces", taskId, sessionOffset, resultOffset],
    queryFn: () => getTaskLearningTraces(taskId, {session_offset: sessionOffset, result_offset: resultOffset}),
    refetchInterval: query => query.state.data?.running || learningActive ? 5000 : false });
  const [jobId, setJobId] = useState<string | null>(null);
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [role, setRole] = useState<"all" | "learner" | "reviewer">("all");
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [followLatest, setFollowLatest] = useState(true);
  const data = query.data;
  const session = data?.sessions.find(item => item.job_id === jobId) ?? data?.sessions[0];
  const older = useQuery({ queryKey: ["learning-traces-older", taskId, session?.job_id, beforeEntry],
    queryFn: () => getTaskLearningTraces(taskId, {job_id: session!.job_id, entry_before: beforeEntry!}),
    enabled: !!session && beforeEntry != null, staleTime: Infinity });
  const entryPage = beforeEntry == null ? session : older.data?.sessions[0];
  const entries = entryPage?.entries.filter(item => role === "all" || item.role === role) ?? [];
  const selected = (!followLatest && entries.find(item => item.ref === selectedRef)) || entries[entries.length - 1];
  const artifact = useQuery({ queryKey: ["learning-trace-artifact", taskId, selected?.ref],
    queryFn: () => getArtifactJson<unknown>(selected!.ref, taskId), enabled: !!selected && pendingId == null, staleTime: Infinity });
  if (query.isLoading) return <p className="font-mono text-xs text-text-mute">loading learning records…</p>;
  if (query.error) return <p className="text-xs text-err">{String(query.error)}</p>;
  if (!data) return null;
  return <div className="space-y-3">
    <div className="text-xs text-text-mute">{t("Learner 分析和 Reviewer 审查均为模型判断；探索执行和普通复验从各自任务原始记录读取。未记录的结果或 token 成本为 unknown。")}{data.running && <Badge className="ml-2" variant="outline">{t("学习运行中")}</Badge>}</div>
    {data.notes.map((note, index) => <p key={index} className="text-xs text-amber">{note}</p>)}
    <div className="flex flex-wrap items-center gap-2">
      <label className="font-mono text-xs">{t("学习会话")}<select value={session?.job_id || ""} onChange={event => {
        setJobId(event.target.value); setSelectedRef(null); setPendingId(null); setFollowLatest(true); setBeforeEntry(null);
      }} className="ml-2 rounded border border-border bg-bg-2 p-1">
        {data.sessions.map(item => <option key={item.job_id} value={item.job_id}>{item.job_id.slice(0, 12)} · {formatWallTime(item.updated_at)}{item.origin.startsWith("legacy") ? " · historical" : ""}</option>)}
      </select></label>
      {(["all", "learner", "reviewer"] as const).map(value => <Button key={value} size="sm" variant={role === value ? "secondary" : "ghost"}
        onClick={() => { setRole(value); setPendingId(null); }}>{value === "all" ? t("全部") : value === "learner" ? "Learner" : "Reviewer"}</Button>)}
      <Button size="sm" variant="ghost" onClick={() => void query.refetch()} disabled={query.isFetching}>{t("刷新记录")}</Button>
      <label className="ml-auto text-xs"><input type="checkbox" checked={followLatest} onChange={event => { setFollowLatest(event.target.checked); if (event.target.checked) setBeforeEntry(null); }} /> {t("跟随最新记录")}</label>
    </div>
    {data.session_total > data.session_limit && <div className="flex items-center gap-2 text-xs">
      <Button size="sm" variant="ghost" disabled={sessionOffset === 0} onClick={() => { setSessionOffset(Math.max(0,sessionOffset - data.session_limit)); setJobId(null); setBeforeEntry(null); }}>{t("上一页会话")}</Button>
      <span>{sessionOffset + 1}–{Math.min(sessionOffset + data.session_limit,data.session_total)} / {data.session_total} {t("个会话")}</span>
      <Button size="sm" variant="ghost" disabled={sessionOffset + data.session_limit >= data.session_total} onClick={() => { setSessionOffset(sessionOffset + data.session_limit); setJobId(null); setBeforeEntry(null); }}>{t("下一页会话")}</Button>
    </div>}
    {session && <p className="font-mono text-xs text-text-mute">{learningCost(session.cost)} · {session.latest_phase === "learning_outcome" ? t("结束时累计成本") : t("会话累计采样快照")}{t("，不按条目相加 · 记录至")}{formatWallTime(session.updated_at)}</p>}
    {session?.histories.length ? <div className="flex flex-wrap gap-2 font-mono text-xs">
      {session.histories.map(history => history.available
        ? <Link key={history.namespace} to={`/tasks/${encodeURIComponent(history.task_id)}`} className="text-cyan hover:underline">{history.namespace} · {history.status || "unknown"} {t("· 打开执行轨迹")}</Link>
        : <span key={history.namespace} className="text-text-mute">{history.namespace} {t("· 原任务不可用，执行结果 unknown")}</span>)}
    </div> : null}
    {data.pending.length > 0 && <div className="flex flex-wrap gap-2">{data.pending.map(item => <Button key={item.id} size="sm" variant={pendingId === item.id ? "secondary" : "ghost"}
      onClick={() => setPendingId(item.id)} title={item.gist}>{t("候选")}{item.id} · {item.verdict || "unknown"} · {item.status}</Button>)}</div>}
    {session && <div className="flex items-center gap-2 text-xs text-text-mute">
      <span>{t("显示")}{entryPage?.entries.length ?? 0} / {session.entry_total} {t("条轨迹；其余保留在原始记录中。")}</span>
      <Button size="sm" variant="ghost" disabled={older.isFetching || !entryPage?.older_entry_count}
        onClick={() => { setBeforeEntry(entryPage!.entries[0].order); setSelectedRef(null); setPendingId(null); setFollowLatest(false); }}>{t("更早记录")}</Button>
      {beforeEntry != null && <Button size="sm" variant="ghost" onClick={() => { setBeforeEntry(null); setSelectedRef(null); setFollowLatest(true); }}>{t("最新记录")}</Button>}
      {older.error && <span className="text-err">{String(older.error)}</span>}
    </div>}
    {data.pending_omitted_count > 0 && <Link className="text-xs text-cyan" to="/skills?tab=pending">{t("另有")}{data.pending_omitted_count} {t("个候选，打开现有技能列表查看")}</Link>}
    <div className="grid gap-3 lg:grid-cols-[200px_1fr]">
      <Card className="min-w-0 lg:max-h-[65vh] overflow-auto"><CardContent className="p-2 space-y-1">
        {entries.map(item => <button key={item.ref} type="button" aria-pressed={selected?.ref === item.ref && !pendingId}
          onClick={() => { setSelectedRef(item.ref); setPendingId(null); setFollowLatest(false); }}
          className={`w-full rounded p-2 text-left font-mono text-[11px] ${selected?.ref === item.ref && !pendingId ? "bg-bg-2 text-cyan" : "text-text-mute hover:bg-bg-2"}`}>
          <div>#{item.order} {item.role === "learner" ? "Learner" : "Reviewer"}</div>
          <div>{item.phase} · {item.status}</div><div>{formatWallTime(item.created_at)}</div>
        </button>)}
        {entries.length === 0 && <p className="text-xs text-text-mute p-2">{t("没有已持久化的")}{role === "all" ? t("学习") : role}{t("记录。历史缺失不代表未执行。")}</p>}
      </CardContent></Card>
      <Card className="min-w-0"><CardContent className="p-3 space-y-2">
        {pendingId ? <PendingReview id={pendingId} /> : selected ? <>
          <p className="font-mono text-xs">{selected.role === "learner" ? "Learner" : "Reviewer"} · {selected.model || "model unknown"} · {selected.phase}</p>
          {artifact.isLoading ? <p>loading transcript…</p> : artifact.error ? <p className="text-err">{String(artifact.error)}</p> : <Transcript value={artifact.data} />}
        </> : <p className="text-xs text-text-mute">{t("此任务尚无可展示的学习会话。启动个人优化后，记录会在此更新。")}</p>}
      </CardContent></Card>
    </div>
    {data.results.map(result => <ResultRecord key={result.ref} taskId={taskId} record={result} />)}
    {data.result_total > data.result_limit && <div className="flex items-center gap-2 text-xs">
      <Button size="sm" variant="ghost" disabled={resultOffset === 0} onClick={() => setResultOffset(Math.max(0,resultOffset - data.result_limit))}>{t("上一页结果")}</Button>
      <span>{resultOffset + 1}–{Math.min(resultOffset + data.result_limit,data.result_total)} / {data.result_total} {t("个结束结果")}</span>
      <Button size="sm" variant="ghost" disabled={resultOffset + data.result_limit >= data.result_total} onClick={() => setResultOffset(resultOffset + data.result_limit)}>{t("下一页结果")}</Button>
    </div>}
  </div>;
}
function ResultRecord({ taskId, record }: { taskId: string; record: { ref: string; status: string; reason: string | null; cost: Record<string, unknown> | null } }) {
  useLocale();
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ["learning-result", taskId, record.ref], queryFn: () => getArtifactJson<unknown>(record.ref, taskId), enabled: open, staleTime: Infinity });
  return <details className="rounded border border-border p-2 text-xs" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer">{t("学习结束记录 ·")}{record.status} · {record.reason || "unknown"} · {learningCost(record.cost)}</summary>
    {open && (query.error ? <p className="text-err">{String(query.error)}</p> : <JsonFold title={t("原始结束结果")} value={query.data} open />)}
  </details>;
}
