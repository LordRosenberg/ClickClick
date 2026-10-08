import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useState } from "react";

type ConnectionInfo = {
  config_path: string;
  configuration: string;
  assistant_prompt: string;
  verification_prompt: string;
  shell: string;
  manual: Array<{ id: string; label: string; command?: string; config_path?: string; guidance: string }>;
};

export function AssistantConnectionPanel({ request }: {
  request: <T>(path: string, body?: unknown) => Promise<T>;
}) {
  const { locale } = useLocale();
  const [info, setInfo] = useState<ConnectionInfo>();
  const [client, setClient] = useState("codex");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const buttonClass = "setup-button";
  useEffect(() => {
    let active = true;
    request<ConnectionInfo>("mcp-connection", { locale }).then(data => {
      if (active) { setInfo(data); setError(""); }
    }).catch(error => { if (active) setError(error instanceof Error ? error.message : t("无法生成接入信息")); });
    return () => { active = false; };
  }, [request, locale]);
  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setNotice(t("已复制")); }
    catch { setNotice(t("无法自动复制，请选中下方文本手动复制。")); }
  }
  const manual = info?.manual.find(item => item.id === client);
  return <section className="setup-card space-y-5">
    <h2 className="text-xl font-semibold">{t("在 AI 助手中添加 ClickClick")}</h2>
    <p className="text-sm text-text-mute">{t("让助手添加，或按它支持的方式手动添加。这里提供接入信息，注册由助手或你完成。")}</p>
    {error && <p role="alert" className="text-sm">{t(error)}</p>}
    {!info && !error && <p className="text-sm text-text-mute">{t("正在生成本机接入信息…")}</p>}
    {info && <>
      <div className="space-y-4 rounded-xl border border-neon/25 bg-neon/[0.04] p-5">
        <h3 className="text-sm font-semibold">{t("让助手添加（推荐）")}</h3>
        <p className="text-xs text-text-mute">{t("把下面的提示词发给能执行本机命令、读取本机文件的助手。接入文件已生成，使用 stdio，无需填写监听地址或认证信息。")}</p>
        <p className="break-all text-xs text-text-mute">{t("本机接入文件：")}{info.config_path}</p>
        <button className={`${buttonClass} setup-button-primary`} onClick={() => void copy(info.assistant_prompt)}>{t("复制助手接入提示词")}</button>
        <details><summary className="cursor-pointer text-sm">{t("查看提示词")}</summary>
          <textarea aria-label={t("助手接入提示词")} readOnly value={info.assistant_prompt} rows={9} className="mt-3 w-full rounded border border-border bg-bg-base p-3 text-xs" />
        </details>
      </div>
      <details className="rounded border border-border p-4">
        <summary className="cursor-pointer text-sm font-semibold">{t("手动添加")}</summary>
        <div className="mt-3 space-y-3">
          <label className="block text-sm">{t("使用的助手")}<select aria-label={t("使用的助手")} className="mt-2 w-full rounded border border-border bg-bg-base px-3 py-2 text-sm" value={client} onChange={event => setClient(event.target.value)}>
            {info.manual.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select></label>
          <p className="text-xs text-text-mute">{manual?.guidance}</p>
          {manual?.command ? <>
            <p className="text-xs text-text-mute">{t("在本机")}{info.shell} {t("中执行：")}</p>
            <textarea aria-label={t("MCP 注册命令")} readOnly value={manual.command} rows={4} className="w-full rounded border border-border bg-bg-base p-3 font-mono text-xs" />
            <button className={buttonClass} onClick={() => void copy(manual.command!)}>{t("复制注册命令")}</button>
          </> : <>
            {manual?.config_path && <p className="break-all text-xs">{t("助手配置文件：")}{manual.config_path}</p>}
            <textarea aria-label={t("MCP 配置内容")} readOnly value={info.configuration} rows={9} className="w-full rounded border border-border bg-bg-base p-3 font-mono text-xs" />
            <button className={buttonClass} onClick={() => void copy(info.configuration)}>{t("复制配置内容")}</button>
          </>}
        </div>
      </details>
      <div className="space-y-2 border-t border-border pt-3">
        <h3 className="text-sm font-semibold">{t("添加后验证连接")}</h3>
        <p className="text-xs text-text-mute">{t("按助手要求重新加载 MCP；如需重启客户端或新开会话，完成后在新会话发送下面的指令。注册完成还不代表连接成功。")}</p>
        <p className="rounded bg-bg-base p-3 text-sm">{info.verification_prompt}</p>
        <button className={buttonClass} onClick={() => void copy(info.verification_prompt)}>{t("复制连接验证指令")}</button>
      </div>
    </>}
    {notice && <p role="status" className="text-xs text-neon">{t(notice)}</p>}
  </section>;
}
