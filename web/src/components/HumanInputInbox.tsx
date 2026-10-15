import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";

type Question = { id: string; question: string; reason?: string; options?: { id: string; label: string; description?: string }[] };
type Interaction = { interaction_id: string; task_id: string; purpose: "clarification" | "confirmation";
  questions: Question[]; owner: string; owner_version: number; can_answer: boolean; fallback_reason?: string; deadline_at: number };

export async function humanRequest<T>(path: string, body?: unknown): Promise<T> {
  let token = sessionStorage.getItem("clickclick-setup-token");
  if (!token) {
    const access = await fetch("/api/setup/access");
    if (!access.ok) throw new Error(t("无法连接回答入口"));
    token = (await access.json()).token as string;
    sessionStorage.setItem("clickclick-setup-token", token);
  }
  const response = await fetch("/api/hitl/" + path, { method: body === undefined ? "GET" : "POST",
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}

export function HumanInputInbox() {
  useLocale();
  const location = useLocation();
  const qc = useQueryClient();
  const pending = useQuery({ queryKey: ["human-input"], queryFn: () => humanRequest<{ interactions: Interaction[] }>("pending"), refetchInterval: 2000 });
  const [openId, setOpenId] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, { text: string; option_id?: string }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const seen = useRef(new Set<string>());
  const responseRetry = useRef<{ signature: string; response_id: string }>();
  const dialog = useRef<HTMLDialogElement>(null);
  const interactions = pending.data?.interactions ?? [];
  const current = interactions.find(item => item.interaction_id === openId);
  const currentVersion = current?.owner_version;
  useEffect(() => {
    if (current && dialog.current && !dialog.current.open) dialog.current.showModal();
  }, [current?.interaction_id]);
  useEffect(() => {
    const next = interactions.find(item => item.can_answer && location.pathname === `/tasks/${item.task_id}` &&
      !seen.current.has(`${item.interaction_id}:${item.owner_version}`));
    if (next && !openId) {
      seen.current.add(`${next.interaction_id}:${next.owner_version}`);
      setOpenId(next.interaction_id);
    }
  }, [interactions, location.pathname, openId]);
  useEffect(() => { setAnswers({}); setError(""); responseRetry.current = undefined; }, [openId, currentVersion]);
  useEffect(() => {
    if (current?.can_answer) humanRequest(`tasks/${current.task_id}/present`, {
      interaction_id: current.interaction_id, owner_version: current.owner_version,
    }).catch(() => qc.invalidateQueries({ queryKey: ["human-input"] }));
  }, [current?.interaction_id, current?.can_answer, currentVersion, qc]);
  useEffect(() => { if (openId && pending.isSuccess && !current) setOpenId(null); }, [openId, current, pending.isSuccess]);
  async function submit(outcome: string) {
    if (!current || busy) return;
    const body = { interaction_id: current.interaction_id, owner_version: current.owner_version, outcome,
      answers: outcome === "answered" ? current.questions.map(q => ({ question_id: q.id, ...answers[q.id] })) : [] };
    const signature = JSON.stringify(body);
    if (responseRetry.current?.signature !== signature) responseRetry.current = { signature, response_id: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      await humanRequest(`tasks/${current.task_id}/reply`, { ...body, response_id: responseRetry.current.response_id });
      setOpenId(null);
      for (const key of ["human-input", "tasks", "task", "timeline", "devices"]) qc.invalidateQueries({ queryKey: [key] });
    } catch (e) { setError(String(e)); await pending.refetch(); }
    finally { setBusy(false); }
  }
  if (!interactions.length) return null;
  return <>
    <aside aria-label={t("待回答的问题")} className="human-input mb-4 rounded-xl border border-neon/30 bg-bg-2 p-4">
      <p className="text-sm">{t("有 {value0} 个任务等待回答", { value0: interactions.length })}</p>
      <div className="mt-2 flex flex-wrap gap-3">{interactions.map(item => <div key={item.interaction_id} className="flex items-center gap-2 text-xs">
        <Link to={`/tasks/${item.task_id}`} className="text-cyan">{t("查看任务")} {item.task_id.slice(0, 8)}</Link>
        <button className="setup-button" onClick={() => setOpenId(item.interaction_id)}>{item.can_answer ? t("回答") : t("查看回答入口")}</button>
      </div>)}</div>
    </aside>
    {current && <dialog ref={dialog} aria-modal="true" aria-labelledby="human-input-title" onCancel={e => { e.preventDefault(); if (!busy) setOpenId(null); }} className="human-input fixed inset-0 z-50 m-auto w-full max-w-xl border-0 bg-transparent p-4 text-text backdrop:bg-black/65">
      <section className="max-h-[85vh] w-full max-w-xl overflow-y-auto rounded-xl border border-border bg-bg-base p-6 shadow-xl">
        <div className="flex items-center justify-between gap-3"><h2 id="human-input-title" className="text-lg">{t(current.purpose === "confirmation" ? "请确认这次操作" : "需要你的选择")}</h2>
          <button disabled={busy} className="setup-button" onClick={() => setOpenId(null)}>{t("稍后回答")}</button></div>
        <Link className="mt-2 block text-xs text-cyan" to={`/tasks/${current.task_id}`}>{t("查看任务")} {current.task_id.slice(0, 8)}</Link>
        {!current.can_answer ? <div className="mt-5 space-y-4"><p>{t("当前回答入口：{value0}", { value0: current.owner })}</p>
          <button className="setup-button" disabled={busy} onClick={async () => { setBusy(true); try {
            await humanRequest(`tasks/${current.task_id}/takeover`, { interaction_id: current.interaction_id, owner_version: current.owner_version }); await pending.refetch();
          } catch (e) { setError(String(e)); } finally { setBusy(false); } }}>{t("转到控制台回答")}</button></div> : <form onSubmit={e => { e.preventDefault(); void submit("answered"); }}>
          {current.questions.map(q => <fieldset key={q.id} className="mt-5 space-y-3"><legend className="whitespace-pre-wrap text-sm font-medium">{q.question}</legend>
            {q.reason && <p className="whitespace-pre-wrap text-xs text-text-mute">{q.reason}</p>}
            {current.purpose === "clarification" && <>
              {q.options?.map(option => <label key={option.id} className="flex cursor-pointer gap-3 rounded-lg border border-border p-3 text-sm">
                <input type="radio" name={q.id} disabled={busy} checked={answers[q.id]?.option_id === option.id}
                  onChange={() => setAnswers(previous => ({ ...previous, [q.id]: { text: option.label, option_id: option.id } }))} />
                <span>{option.label}{option.description && <span className="mt-1 block text-xs text-text-mute">{option.description}</span>}</span></label>)}
              <label className="block text-xs">{t("也可以自行回答")}<textarea className="setup-input mt-2 min-h-20 w-full" maxLength={4096} disabled={busy} required
                value={answers[q.id]?.text ?? ""} onChange={e => setAnswers(previous => ({ ...previous, [q.id]: { text: e.target.value } }))} /></label>
            </>}
          </fieldset>)}
          <div className="mt-6 flex flex-wrap gap-2">
            {current.purpose === "confirmation" ? <><button type="button" className="setup-button setup-button-primary" disabled={busy} onClick={() => submit("confirmed")}>{t("确认这次操作")}</button>
              <button type="button" className="setup-button" disabled={busy} onClick={() => submit("declined")}>{t("不允许")}</button></> :
              <button type="submit" className="setup-button setup-button-primary" disabled={busy || current.questions.some(q => !answers[q.id]?.text.trim())}>{t("提交回答并继续")}</button>}
            <button type="button" className="setup-button" disabled={busy} onClick={() => submit("unable_to_answer")}>{t("无法回答")}</button>
            <button type="button" className="setup-button" disabled={busy} onClick={() => submit("cancel_task")}>{t("取消任务")}</button>
          </div></form>}
        <p className="mt-4 text-xs text-text-mute">{t("稍后回答不会取消任务。等待不会延长任务时限。")}</p>
        {error && <p role="alert" className="mt-3 break-words text-sm text-err">{error}</p>}
      </section>
    </dialog>}
  </>;
}
