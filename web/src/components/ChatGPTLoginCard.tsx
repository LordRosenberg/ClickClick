import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ChatGPTStatus, ChatGPTLoginStart, ChatGPTLoginPoll } from "@/api/types";
import type { SetupRequest } from "@/lib/console-settings";

export function ChatGPTLoginCard({ request, beforeLogin, disabled }: { request: SetupRequest; beforeLogin: () => Promise<void>; disabled: boolean }) {
  useLocale();
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["chatgpt", "status"], queryFn: () => request<ChatGPTStatus>("chatgpt/status"), staleTime: 0 });
  const [pending, setPending] = useState<ChatGPTLoginStart | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const generation = useRef(0);
  const active = useRef(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false; generation.current++;
      if (timer.current !== null) clearTimeout(timer.current);
      if (active.current) void request("chatgpt/login/cancel", {}).catch(() => {});
    };
  }, [request]);

  async function cancel() {
    generation.current++;
    if (timer.current !== null) clearTimeout(timer.current);
    try { await request("chatgpt/login/cancel", {}); active.current = false; setPending(null); setBusy(false); setMessage(t("已停止等待授权，可重新登录。")); }
    catch { setMessage(t("未能停止后台授权，请检查连接后重试。")); }
  }

  function poll(login: ChatGPTLoginStart, started: number, attempt: number) {
    timer.current = setTimeout(async () => {
      if (!alive.current || generation.current !== attempt) return;
      if (Date.now() - started > 15 * 60_000) { await cancel(); setMessage(t("授权超时，请重新登录。")); return; }
      try {
        const result = await request<ChatGPTLoginPoll>("chatgpt/login/poll", {});
        if (!alive.current || generation.current !== attempt) return;
        if (result.status === "pending") { poll(login, started, attempt); return; }
        active.current = false; setPending(null); setBusy(false);
        setMessage(result.status === "authenticated" ? t("授权完成，登录状态已保存在本机，可以派发手机任务。") : t("授权未完成，请重新登录。"));
        await qc.invalidateQueries({ queryKey: ["chatgpt", "status"] });
      } catch {
        if (!alive.current || generation.current !== attempt) return;
        setMessage(t("暂时无法查询授权状态，正在重试；也可以停止后重新登录。"));
        poll(login, started, attempt);
      }
    }, Math.max(3, login.interval_s || 5) * 1000);
  }

  async function login() {
    // Reserve the window during the actual click, before asynchronous saves.
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    setBusy(true); setMessage("");
    const attempt = ++generation.current;
    try {
      await beforeLogin();
      if (!alive.current || generation.current !== attempt) { popup?.close(); return; }
      const value = await request<ChatGPTLoginStart>("chatgpt/login/start", {});
      active.current = true;
      if (!alive.current || generation.current !== attempt) { popup?.close(); await request("chatgpt/login/cancel", {}); return; }
      const url = new URL(value.verify_url);
      if (url.protocol !== "https:" || url.hostname !== "auth.openai.com") throw new Error(t("授权地址不符合预期，请重试。"));
      setPending(value);
      if (popup) popup.location.href = value.verify_url;
      else setMessage(t("浏览器未打开新窗口，请点击下面的授权页链接。"));
      poll(value, Date.now(), attempt);
    } catch (error) {
      popup?.close();
      if (active.current) { await request("chatgpt/login/cancel", {}).catch(() => {}); active.current = false; }
      if (alive.current) { setBusy(false); setMessage(error instanceof Error ? error.message : t("无法发起登录，请重试。")); }
    }
  }

  return <div className="space-y-4 rounded-xl border border-border bg-bg-base/30 p-5 text-sm">
    <p className="flex items-center gap-2 font-medium"><span className={`h-2 w-2 rounded-full ${status.data?.authenticated ? "bg-neon" : "bg-text-faint"}`} />{t("订阅登录 ·")}{status.isLoading ? t("检查中…") : status.data?.authenticated ? t("已登录") : t("未登录")}</p>
    <p className="text-text-mute">{t("点击后保存当前设置并打开 OpenAI 授权页。授权完成后，ClickClick 自动保存本机登录状态。")}</p>
    {!status.data?.authenticated && <div className="rounded-lg bg-neon/5 p-4">
      <p className="font-semibold text-neon">{t("设备授权码会显示在这个 ClickClick 页面")}</p>
      <p className="mt-1 text-text-mute">{t("点击“登录并授权”后，在这里复制设备授权码，再粘贴到 OpenAI 授权页。若跳转后要求输入验证码，请返回此页获取。")}</p>
    </div>}
    {!pending && <button type="button" className={`setup-button ${status.data?.authenticated ? "" : "setup-button-primary"}`} disabled={busy || disabled} onClick={login}>{busy ? t("准备登录…") : status.data?.authenticated ? t("重新登录并授权") : t("登录并授权")}</button>}
    {pending && <div className="space-y-3">
      <div className="rounded border border-neon/40 bg-neon/5 p-4">
        <p>{t("设备授权码（填入 OpenAI 授权页）")}</p>
        <strong className="mt-2 block select-all font-mono text-2xl tracking-widest text-neon">{pending.user_code}</strong>
      </div>
      <div className="flex flex-wrap gap-4">
        <button type="button" className="text-cyan" onClick={async () => {
          try { await navigator.clipboard.writeText(pending.user_code); setMessage(t("设备授权码已复制，请粘贴到 OpenAI 授权页。")); }
          catch { setMessage(t("请选中上方设备授权码手动复制，粘贴到 OpenAI 授权页。")); }
        }}>{t("复制设备授权码")}</button>
        <a className="text-cyan" href={pending.verify_url} target="_blank" rel="noreferrer">{t("打开授权页")}</a>
        <button type="button" className="text-text-mute" onClick={cancel}>{t("停止等待")}</button>
      </div><p className="text-text-mute">{t("正在等待浏览器授权…完成后会自动更新，无需重启。")}</p>
    </div>}
    {message && <p role="status" className="text-cyan">{message}</p>}
    {status.error && <p role="alert" className="text-err">{t("无法读取本机登录状态，请检查后台连接。")}</p>}
  </div>;
}
