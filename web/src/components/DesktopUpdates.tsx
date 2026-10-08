import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useState } from "react";

type UpdateStatus = {
  supported: boolean;
  current_version?: string;
  auto_check?: boolean;
  maintenance?: boolean;
  feed?: { checked_at?: number; error?: string; available?: { version: string; notes: string; release_url: string } };
  operation?: { state?: string; version?: string; error?: string };
};

export function DesktopUpdates({ token }: { token: string }) {
  useLocale();
  const [status, setStatus] = useState<UpdateStatus>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [connected, setConnected] = useState(true);
  const states: Record<string, string> = { queued: t("等待更新器"), downloading: t("下载并校验安装包"), installing: t("正在升级并重启后台"), succeeded: t("升级成功"), failed: t("更新未完成") };

  async function request(path = "", body?: unknown) {
    const response = await fetch(`/api/setup/updates${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || t("更新操作未完成"));
    setConnected(true);
    setStatus(result);
  }

  useEffect(() => {
    let alive = true;
    async function refresh() {
      try {
        const response = await fetch("/api/setup/updates", { headers: { Authorization: `Bearer ${token}` } });
        if (!response.ok) throw new Error();
        const value = await response.json();
        if (alive) { setStatus(value); setConnected(true); }
      } catch { if (alive) setConnected(false); }
    }
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [token]);

  async function act(path: string, body: unknown) {
    setBusy(true); setMessage("");
    try { await request(path, body); }
    catch (error) { setMessage(error instanceof Error ? error.message : t("更新操作未完成")); }
    finally { setBusy(false); }
  }

  if (!status?.supported) return null;
  const offered = status.feed?.available;
  const operation = status.operation;
  const active = ["queued", "downloading", "installing"].includes(operation?.state ?? "");
  const button = "rounded border border-neon/40 px-3 py-2 text-sm text-neon disabled:opacity-40";
  return <section className="space-y-3 rounded border border-border bg-bg-2 p-5">
    <h2 className="font-semibold">{t("版本更新")}</h2>
    <p className="text-sm">{t("当前版本：")}{status.current_version}</p>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={status.auto_check ?? true} disabled={busy}
      onChange={e => act("/preferences", { auto_check: e.target.checked })} />{t("自动检查新版（只检查版本信息，升级需确认）")}</label>
    <button className={button} disabled={busy || active} onClick={() => act("/check", {})}>{t("检查更新")}</button>
    {status.feed?.checked_at && <p className="text-xs text-text-mute">{t("上次检查：")}{new Date(status.feed.checked_at * 1000).toLocaleString()}</p>}
    {status.feed?.error && <p role="status" className="text-sm text-amber">{status.feed.error}</p>}
    {offered && <div className="space-y-2">
      <p>{t("发现新版")}{offered.version} · <a className="text-cyan underline" href={offered.release_url} target="_blank" rel="noreferrer">{t("查看发布说明")}</a></p>
      {offered.notes && <p className="whitespace-pre-wrap text-sm text-text-mute">{offered.notes}</p>}
      <p className="text-sm text-text-mute">{t("升级将重启后台。请先完成、暂停或取消手机任务；API 配置、任务记录和用户技能会保留。")}</p>
      <button className={button} disabled={busy || active || status.maintenance} onClick={() => act("/install", { version: offered.version, confirmed: true })}>{t("确认升级至")}{offered.version}</button>
    </div>}
    {!offered && status.feed?.checked_at && !status.feed.error && <p className="text-sm">{t("暂无适用于当前平台的更新。")}</p>}
    {operation?.state && <p role="status" className="text-sm">{states[operation.state] ?? operation.state}{operation.version ? ` · ${operation.version}` : ""}</p>}
    {operation?.error && <p className="text-sm text-amber">{operation.error}</p>}
    {!connected && <p role="status" className="text-sm">{t("后台连接中断，正在重连。更新完成后会自动刷新状态；持续无法连接时请查看安装目录的 data/service.log。")}</p>}
    {(status.maintenance || active) && <button className={button} disabled={busy} onClick={() => act("/recover", { confirmed: true })}>{t("确认恢复更新状态（仅在更新器已退出时）")}</button>}
    {message && <p role="status" className="text-sm text-amber">{message}</p>}
  </section>;
}
