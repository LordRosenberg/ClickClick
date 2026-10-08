import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import type { JevSettings } from "@/lib/console-settings";

export function JevSettingsPanel({ value, onChange }: { value: JevSettings; onChange: (value: JevSettings) => void }) {
  useLocale();
  const input = "mt-1 w-full rounded border border-border bg-bg-base px-3 py-2 text-sm";
  const update = (patch: Partial<JevSettings>) => onChange({ ...value, ...patch });
  return <div className="space-y-3 rounded border border-border p-4">
    <label className="block text-sm font-semibold"><input type="checkbox" checked={value.mode !== "off"}
      onChange={event => update({ mode: event.target.checked ? "enforce" : "off" })} /> {t("启用 Jev 摘要检查（默认关闭）")}</label>
    <p className="text-xs text-text-mute">{t("Jev 检查压缩后的任务摘要是否忠实于历史记录。开启后会将摘要和相关文字证据发送至下面的 Jev API，产生额外请求；关闭后不调用 Jev。保存设置不会发起测试请求。")}</p>
    <details open={value.mode !== "off"}>
      <summary className="cursor-pointer text-sm">{t("Jev API 配置")}</summary>
      <div className="mt-3 space-y-3">
        <label className="block text-sm">{t("Jev API 地址")}<input className={input} value={value.base_url} onChange={event => update({ base_url: event.target.value })} /></label>
        <label className="block text-sm">Jev API Key{value.api_key_configured ? t("（已保存，留空保留）") : ""}<input className={input} type="password" autoComplete="off" value={value.api_key ?? ""} onChange={event => update({ api_key: event.target.value })} /></label>
        <p className="text-xs text-text-mute">{t("Jev 使用独立的 API Key；更换 API 地址时请重新填写 Key。")}</p>
        <label className="block text-sm">{t("Jev 模型 ID")}<input className={input} value={value.model} onChange={event => update({ model: event.target.value })} /></label>
        <details><summary className="cursor-pointer text-sm">{t("Jev 高级设置")}</summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {value.mode !== "off" && <label className="text-sm">{t("检查模式")}<select className={input} value={value.mode} onChange={event => update({ mode: event.target.value as JevSettings["mode"] })}>
              <option value="enforce">{t("参与摘要接纳")}</option><option value="shadow">{t("仅记录检查结果")}</option>
            </select></label>}
            <label className="text-sm">{t("Jev 请求超时（秒）")}<input className={input} type="number" min={0.1} max={60} step={0.1} value={value.timeout_s} onChange={event => update({ timeout_s: Number(event.target.value) })} /></label>
            <label className="text-sm">{t("Jev 服务异常时")}<select className={input} value={value.failure_policy} onChange={event => update({ failure_policy: event.target.value as JevSettings["failure_policy"] })}>
              <option value="bypass">{t("跳过检查，保留降级记录")}</option><option value="rollback">{t("保留原始历史，不接纳摘要")}</option>
            </select></label>
          </div>
        </details>
      </div>
    </details>
  </div>;
}
