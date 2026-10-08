import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useCallback, useEffect, useState } from "react";
import { CoreSettingsPanel } from "@/components/CoreSettingsPanel";
import { AssistantConnectionPanel } from "@/components/AssistantConnectionPanel";
import { DesktopUpdates } from "@/components/DesktopUpdates";

type SetupStatus = {
  api_model: { saved: boolean; model?: string; base_url?: string };
  guides: Record<string, string>;
  guidance: string;
};

export function SetupView() {
  useLocale();
  const [token, setToken] = useState(() => {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const supplied = fragment.get("token");
    if (supplied) {
      sessionStorage.setItem("clickclick-setup-token", supplied);
      history.replaceState(null, "", location.pathname + location.search);
    }
    return supplied ?? sessionStorage.getItem("clickclick-setup-token") ?? "";
  });
  const [status, setStatus] = useState<SetupStatus>();
  const [pairAddress, setPairAddress] = useState("");
  const [pairCode, setPairCode] = useState("");
  const [connectAddress, setConnectAddress] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [inventory, setInventory] = useState<Array<{ serial: string; state: string }>>([]);

  const request = useCallback(async <T,>(path: string, body?: unknown): Promise<T> => {
    const response = await fetch(`/api/setup/${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : t("请求未完成，请检查填写内容。"));
    return data as T;
  }, [token]);

  useEffect(() => {
    if (token) return;
    fetch("/api/setup/access").then(async response => {
      if (!response.ok) throw new Error(t("请在这台电脑通过 localhost 打开 Console 设置；也可使用安装器的设置入口。"));
      const data = await response.json();
      sessionStorage.setItem("clickclick-setup-token", data.token);
      setToken(data.token);
    }).catch(error => setNotice(error.message));
  }, [token]);

  useEffect(() => {
    if (!token) return;
    request<SetupStatus>("status").then(data => {
      setStatus(data);
    }).catch((error: Error) => setNotice(error.message));
  }, [token, request]);

  async function act(action: () => Promise<void>) {
    setBusy(true); setNotice("");
    try { await action(); } catch (error) { setNotice(error instanceof Error ? error.message : t("操作未完成")); }
    finally { setBusy(false); }
  }

  const inputClass = "w-full rounded border border-border bg-bg-base px-3 py-2 text-sm";
  const buttonClass = "rounded border border-neon/40 px-3 py-2 text-sm text-neon disabled:opacity-40";
  const cardClass = "space-y-3 rounded border border-border bg-bg-2 p-5";

  if (!token) return <div className={cardClass}>
    <h1 className="text-lg font-semibold">{t("设置")}</h1>
    <p>{notice || t("正在连接本机设置…")}</p>
  </div>;

  return <div className="mx-auto max-w-3xl space-y-5">
    <div><h1 className="text-xl font-semibold">{t("设置")}</h1>
      <p className="mt-2 text-sm text-text-mute">{t("选择订阅登录或 API，设置任务预算，连接手机，在 AI 助手中添加 ClickClick。")}</p></div>
    {notice && <div role="status" className="whitespace-pre-wrap rounded border border-cyan/40 p-4 text-sm">{t(notice)}</div>}
    {notice && <button className={buttonClass} onClick={() => {
      sessionStorage.removeItem("clickclick-setup-token"); setToken(""); setNotice("");
    }}>{t("重新连接本机设置")}</button>}
    <CoreSettingsPanel request={request} />
    <section className={cardClass}>
      <h2 className="font-semibold">{t("连接 Android")}</h2>
      <details open><summary>{t("Wi-Fi（Android 11 及以上，不需要数据线）")}</summary>
        <p className="my-3 text-sm text-text-mute">{t(status?.guides.wifi ?? "")}</p>
        <label className="block text-sm">{t("配对地址与端口")}<input className={inputClass} placeholder={t("手机配对码页面显示的 IP:端口")} value={pairAddress} onChange={e => setPairAddress(e.target.value)} /></label>
        <label className="my-2 block text-sm">{t("六位配对码")}<input className={inputClass} type="password" inputMode="numeric" autoComplete="off" maxLength={6} value={pairCode} onChange={e => setPairCode(e.target.value)} /></label>
        <button className={buttonClass} disabled={busy || pairCode.length !== 6} onClick={() => act(async () => {
          const code = pairCode; setPairCode(""); const data = await request<{ guidance: string }>("pair", { endpoint: pairAddress, code }); setNotice(data.guidance);
        })}>{t("配对电脑与手机")}</button>
        <label className="my-3 block text-sm">{t("连接地址与端口")}<input className={inputClass} placeholder={t("无线调试主页面的 IP:端口，通常不同于配对端口")} value={connectAddress} onChange={e => setConnectAddress(e.target.value)} /></label>
        <button className={buttonClass} disabled={busy || !connectAddress} onClick={() => act(async () => {
          const data = await request<{ devices?: Array<{ serial: string; state: string }>; connected: boolean; guidance: string }>("connect", { endpoint: connectAddress }); setInventory(data.devices ?? []);
          setNotice(data.connected ? t("ADB 已连接。后台会初始化 Collector；让助手调用 get_status 检查环境步骤与待授权事项。") : data.guidance);
        })}>{t("连接设备")}</button>
      </details>
      <details><summary>{t("USB 数据线")}</summary><p className="my-3 text-sm text-text-mute">{t(status?.guides.usb ?? "")}</p></details>
      <details><summary>{t("模拟器")}</summary><p className="my-3 text-sm text-text-mute">{t(status?.guides.emulator ?? "")}</p></details>
      <details><summary>{t("Android 10 及以下使用 Wi-Fi")}</summary><p className="my-3 text-sm text-text-mute">{t(status?.guides.legacy_wifi ?? "")}</p></details>
      <button className={buttonClass} disabled={busy} onClick={() => act(async () => {
        const data = await request<{ devices: Array<{ serial: string; state: string }>; guidance: string }>("devices"); setInventory(data.devices); setNotice(data.guidance);
      })}>{t("检查已连接设备")}</button>
      {inventory.map(d => <p key={d.serial} className="text-sm">{d.serial} · {d.state === "device" ? t("ADB 在线") : d.state}</p>)}
    </section>
    <AssistantConnectionPanel request={request} />
    <DesktopUpdates token={token} />
  </div>;
}
