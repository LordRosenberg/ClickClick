import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { JevSettingsPanel } from "@/components/JevSettingsPanel";
import { ChatGPTLoginCard } from "@/components/ChatGPTLoginCard";
import { getLearningPreferences, updateLearningPreferences } from "@/api/client";
import type { LearningPreferences } from "@/api/client";
import { emptyModel, emptyProfile, renameModel, settingsPayload, modelPresets, roleModelOptions, resolvedRoleModel, selectRoleModel, selectCustomRoleModel, ROLE_LABELS } from "@/lib/console-settings";
import type { Mode, ModelSettings, Profile, Role, RuntimeSettings, SettingsSummary, SetupRequest, JevSettings } from "@/lib/console-settings";
import { KeyRound, UserRound, Check, Save } from "lucide-react";

const input = "setup-input mt-2 w-full";
const button = "setup-button";
const numbers: Array<{ key: Exclude<keyof RuntimeSettings, "agent_architecture">; label: string; min: number; max: number; optional?: boolean }> = [
  { key: "executor_context_tokens", label: "执行历史默认长度（tokens）", min: 1, max: 1000000 },
  { key: "chatgpt_history_tokens", label: "订阅执行历史长度（留空跟随默认）", min: 1, max: 1000000, optional: true },
  { key: "default_task_model_calls", label: "任务模型请求上限（留空使用 200 次保护上限）", min: 1, max: 200, optional: true },
  { key: "default_task_device_actions", label: "任务设备动作上限（留空不额外限制）", min: 1, max: 10000, optional: true },
  { key: "default_task_seconds", label: "任务总时限（秒，留空不额外限制）", min: 1, max: 86400, optional: true },
  { key: "learning_default_calls", label: "Skill 学习模型请求上限", min: 12, max: 64 },
  { key: "learning_default_actions", label: "Skill 学习动作上限", min: 1, max: 100 },
  { key: "learning_default_seconds", label: "Skill 学习总时限（秒）", min: 30, max: 1800 },
];

export function CoreSettingsPanel({ request, section = "models" }: { request: SetupRequest; section?: "models" | "execution" }) {
  useLocale();
  const qc = useQueryClient();
  const [mode, setMode] = useState<Mode>("api");
  const [profiles, setProfiles] = useState<Partial<Record<Mode, Profile>>>({});
  const [runtime, setRuntime] = useState<RuntimeSettings>();
  const [jev, setJev] = useState<JevSettings>({ mode: "off", base_url: "https://api.typesafe.ai", model: "jev-1.13.0", api_key: "", timeout_s: 8, failure_policy: "bypass" });
  const [preferences, setPreferences] = useState<LearningPreferences>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [customModels, setCustomModels] = useState<Set<number>>(new Set());
  const [customRoleNames, setCustomRoleNames] = useState<Partial<Record<Role, string>>>({});
  const [expandedModels, setExpandedModels] = useState<Record<string, boolean>>({});
  const [expandedParameters, setExpandedParameters] = useState<Record<string, boolean>>({});
  const modelCards = useRef(new Map<string, HTMLDetailsElement>());
  const savedProfile = profiles[mode] ?? emptyProfile(mode);
  const profile = savedProfile.models.length ? savedProfile : emptyProfile(mode);
  const presets = modelPresets(mode);
  const roleOptions = roleModelOptions(mode, profile);
  const modelName = (id: string) => presets.find(preset => preset.id === id)?.label ?? (id || t("自定义模型"));
  const modelUsers = (id: string) => (Object.keys(ROLE_LABELS) as Role[])
    .filter(role => role !== "default_model" && resolvedRoleModel(profile, role) === id)
    .map(role => t(ROLE_LABELS[role]));
  function editModelParameters(id: string) {
    setExpandedModels(previous => ({ ...previous, [id]: true }));
    setExpandedParameters(previous => ({ ...previous, [id]: true }));
    requestAnimationFrame(() => {
      const card = modelCards.current.get(id);
      card?.scrollIntoView({ block: "start", behavior: "smooth" });
      card?.querySelector<HTMLElement>("summary")?.focus({ preventScroll: true });
    });
  }

  const load = async () => {
    const value = await request<SettingsSummary>("settings");
    setMode(value.mode); setProfiles(value.profiles);
    const { profiles: _profiles, mode: _mode, jev: jevConfig, ...defaults } = value;
    if (jevConfig) setJev({ ...jevConfig, api_key: "" });
    setRuntime(defaults);
  };
  useEffect(() => {
    let active = true;
    request<SettingsSummary>("settings").then(value => {
      if (!active) return;
      setMode(value.mode); setProfiles(value.profiles);
      const { profiles: _profiles, mode: _mode, jev: jevConfig, ...defaults } = value;
      if (jevConfig) setJev({ ...jevConfig, api_key: "" });
      setRuntime(defaults);
    }).catch(error => { if (active) setNotice(String(error)); });
    getLearningPreferences().then(value => { if (active) setPreferences(value); })
      .catch(() => { if (active) setNotice(t("无法加载 Skill 偏好，请检查后台连接。")); });
    return () => { active = false; };
  }, [request]);

  const changeProfile = (value: Profile) => setProfiles(previous => ({ ...previous, [mode]: value }));
  const updateModel = (index: number, values: Partial<ModelSettings>) => changeProfile({ ...profile,
    models: profile.models.map((model, i) => i === index ? { ...model, ...values } : model) });
  async function save() {
    if (!runtime) throw new Error(t("设置尚未加载"));
    if (Object.keys(customRoleNames).length) throw new Error(t("请先确认或取消自定义模型"));
    await request("settings", settingsPayload(mode, profile, runtime, jev));
    await load();
    await qc.invalidateQueries({ queryKey: ["models"] });
    await qc.invalidateQueries({ queryKey: ["learning-defaults"] });
    setNotice(t("设置已保存，新任务立即使用。模型凭据尚未测试；保存不会调用模型。运行中和暂停的任务需要先结束或取消。"));
  }
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setNotice("");
    try { await action(); } catch (error) { setNotice(error instanceof Error ? error.message : t("未能保存设置")); }
    finally { setBusy(false); }
  }

  function roleCard(role: Role, label: string) {
    const id = resolvedRoleModel(profile, role);
    const selectedModel = profile.models.find(model => model.id === id);
    const customName = customRoleNames[role];
    const clearCustomName = () => setCustomRoleNames(previous => {
      const next = { ...previous }; delete next[role]; return next;
    });
    return <div key={role} className="rounded-xl border border-border bg-bg-base/30 p-4">
      <label className="block text-sm">{t(label)}<select className={input} value={customName !== undefined ? "__custom__" : profile[role]} onChange={e => {
        if (e.target.value === "__custom__") {
          setCustomRoleNames(previous => ({ ...previous, [role]: "" }));
        } else {
          clearCustomName();
          changeProfile(selectRoleModel(profile, role, e.target.value, mode));
        }
      }}>
        <option value="">{role === "default_model" ? t("选择默认模型") : role === "skill_learner_model" ? t("跟随 Planner + Reviewer") : role === "skill_reviewer_model" ? t("跟随 Skill Learner") : t("跟随默认模型")}</option>
        {roleOptions.map(model => <option key={model.id} value={model.id}>{model.label}</option>)}
        <option value="__custom__">{t("自定义模型…")}</option>
      </select></label>
      {customName !== undefined && <div className="mt-4 space-y-3 rounded-lg border border-neon/20 bg-neon/[0.04] p-3">
        <label className="block text-sm">{t("模型名称或 ID")}<input className={input} autoFocus maxLength={200} value={customName}
          placeholder={mode === "api" ? "my-model / openai/my-model" : "my-model / chatgpt/my-model"}
          onChange={e => setCustomRoleNames(previous => ({ ...previous, [role]: e.target.value }))} /></label>
        <p className="text-xs leading-6 text-text-mute">{t(mode === "api" ? "填写服务商提供的模型名；未填写前缀时使用 openai/（兼容 API）。其他服务商请填写完整的服务商/模型 ID。" : "填写账号可用的模型名；未填写前缀时自动使用 chatgpt/。")}</p>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={button} disabled={!customName.trim()} onClick={() => {
            try {
              const next = selectCustomRoleModel(profile, role, customName, mode);
              changeProfile(next); clearCustomName(); setNotice("");
              editModelParameters(next[role]);
            } catch (error) { setNotice(error instanceof Error ? t(error.message) : t("未能保存设置")); }
          }}>{t("使用此模型")}</button>
          <button type="button" className={button} onClick={clearCustomName}>{t("取消")}</button>
        </div>
      </div>}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-text-mute">{selectedModel ? t("{value0} · {value1}", { value0: modelName(id), value1: selectedModel.reasoning_supported ? t("思考强度：{value0}", { value0: selectedModel.reasoning_effort || t("服务默认") }) : t("未设置思考强度") }) : t("请先选择模型")}</p>
        <button type="button" className="text-xs text-neon underline underline-offset-4 disabled:opacity-40" aria-label={t("{value0}：编辑所选模型参数", { value0: t(label) })} disabled={!selectedModel} onClick={() => editModelParameters(id)}>{t("编辑所选模型参数")}</button>
      </div>
    </div>;
  }

  return <section className="setup-card space-y-6">
    <div><h2 className="text-xl font-semibold">{t(section === "models" ? "模型与账号" : "运行与学习")}</h2>
    <p className="mt-2 text-sm leading-6 text-text-mute">{t(section === "models" ? "选择 ClickClick 操作手机时使用的模型。所有角色使用 ClickClick 自己的 harness 和会话。" : "设置每次任务和 Skill 学习的开销，按需开启额外检查。")}</p></div>
    {notice && <p role="status" className="whitespace-pre-wrap text-sm text-cyan">{t(notice)}</p>}
    <fieldset disabled={busy || !runtime} className="space-y-4 disabled:opacity-60">
      <div hidden={section !== "models"} className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">{(["subscription", "api"] as Mode[]).map(value => <label key={value} className={`relative flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition-colors ${mode === value ? "border-neon/50 bg-neon/[0.06]" : "border-border bg-bg-base/40 hover:border-border-hi"}`}>
        <input className="sr-only" type="radio" name="model-mode" checked={mode === value} onChange={() => { setMode(value); setCustomModels(new Set()); setCustomRoleNames({}); setNotice(""); }} />
        {value === "api" ? <KeyRound size={19} className="mt-0.5 text-text-mute" /> : <UserRound size={19} className="mt-0.5 text-text-mute" />}
        <span className="pr-4"><span className="block text-sm font-medium">{value === "api" ? t("配置 API") : t("Codex / ChatGPT 订阅")}</span><span className="mt-1.5 block text-xs text-text-mute">{t(value === "api" ? "使用自己的模型服务与 API Key" : "用现有账号登录并授权")}</span></span>
        {mode === value && <Check size={15} className="absolute right-3 top-4 text-neon" />}
      </label>)}</div>
      <p className="text-sm text-text-mute">{t("切换后点击保存才会生效，两种方式的配置会分别保留。模型需支持图片与工具调用。")}</p>
    {mode === "subscription" && <ChatGPTLoginCard request={request} beforeLogin={save} disabled={busy || !runtime || !profile.default_model} />}
      <p className="text-xs leading-6 text-text-mute">{t("模型的可用性取决于你的账号或 API 服务。")}</p>
      <details open><summary className="cursor-pointer text-sm font-semibold">{t("1. 选择模型")}</summary>
        <div className="mt-3">{roleCard("default_model", ROLE_LABELS.default_model)}</div>
        <details className="mt-4"><summary className="text-xs text-text-mute">{t("为不同角色选择模型（可选）")}</summary>
          <p className="mt-3 text-xs leading-6 text-text-mute">{t("默认共用同一模型。需要时可为规划、执行和 Skill 学习分别选择，参数在下方按模型配置。")}</p>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">{(Object.entries(ROLE_LABELS) as Array<[Role, string]>).filter(([role]) => role !== "default_model").map(([role, label]) => roleCard(role, label))}</div>
        </details>
      </details>
      <div className="border-t border-border pt-4">
        <h3 className="text-sm font-semibold">{t("2. 配置模型参数")}</h3>
        <p className="mt-2 text-xs text-text-mute">{t("在上方选好模型后，在这里设置 API 连接、思考强度等参数。同一模型的配置由使用它的角色共用。")}</p>
      </div>
      {profile.models.map((model, index) => <details key={index}
        ref={card => { if (card) modelCards.current.set(model.id, card); else modelCards.current.delete(model.id); }}
        open={expandedModels[model.id] ?? index === 0}
        onToggle={e => { const open = e.currentTarget.open;
          setExpandedModels(previous => previous[model.id] === open ? previous : { ...previous, [model.id]: open }); }}
        className="scroll-mt-20 rounded-xl border border-border bg-bg-base/40 p-5">
        <summary className="cursor-pointer text-sm font-semibold">{modelName(model.id)} {t("· 模型配置")}{profile.default_model === model.id && <span className="ml-2 rounded bg-neon/10 px-2 py-0.5 text-xs font-normal text-neon">{t("默认")}</span>}
        </summary>
        <div className="mt-3 space-y-3">
        <p className="text-xs text-text-mute">{t("使用此配置的角色：")}{modelUsers(model.id).join(t("、")) || t("暂未分配")}{t("。选择同一模型的角色共用以下参数。")}</p>
        <details open={!model.id || customModels.has(index)}>
          <summary className="cursor-pointer text-xs text-text-mute">{t("更换模型或自定义 ID")}</summary>
          <div className="mt-3 space-y-3">
        <label className="block text-sm">{t("模型")}<select className={input}
          value={!customModels.has(index) && presets.some(preset => preset.id === model.id) ? model.id : "custom"}
          onChange={e => {
            if (e.target.value === "custom") setCustomModels(previous => new Set([...previous, index]));
            else { setCustomModels(previous => { const next = new Set(previous); next.delete(index); return next; });
              changeProfile(renameModel(profile, index, e.target.value)); }
          }}>
          {presets.map(preset => <option key={preset.id} value={preset.id}
            disabled={profile.models.some((other, i) => i !== index && other.id === preset.id)}>{preset.label}</option>)}
          <option value="custom">{t("自定义模型")}</option>
        </select></label>
        {(customModels.has(index) || !presets.some(preset => preset.id === model.id)) && <label className="block text-sm">{t("模型 ID")}<input className={input} value={model.id}
          placeholder={mode === "api" ? t("例如 openai/gpt-4.1") : t("例如 chatgpt/gpt-5.6-sol，以账号实际可用模型为准")}
          onChange={e => changeProfile(renameModel(profile, index, e.target.value))} /></label>}
          </div>
        </details>
        <div className="ml-2 space-y-4 border-l-2 border-neon/25 pl-4">
        {mode === "api" && <fieldset className="space-y-3">
          <legend className="mb-2 text-sm font-medium">{t("此模型的 API 连接配置")}</legend>
          <label className="block text-sm">{t("API 地址")}<input className={input} placeholder="https://api.openai.com/v1" value={model.base_url} onChange={e => updateModel(index, { base_url: e.target.value, credentials_from: undefined })} /></label>
          <label className="block text-sm">API Key{model.api_key_configured ? t("（已保存，留空保留）") : ""}<input className={input} type="password" autoComplete="off" value={model.api_key ?? ""} onChange={e => updateModel(index, { api_key: e.target.value })} /></label>
          <label className="block text-sm">{t("HTTP User-Agent（可选，留空使用默认值）")}<input className={input} value={model.user_agent} onChange={e => updateModel(index, { user_agent: e.target.value })} /></label>
        </fieldset>}
        <details open={expandedParameters[model.id] ?? false}
          onToggle={e => { const open = e.currentTarget.open;
            setExpandedParameters(previous => previous[model.id] === open ? previous : { ...previous, [model.id]: open }); }}>
          <summary className="cursor-pointer text-sm font-medium">{t("此模型的思考与高级参数")}</summary>
          <div className="mt-3 space-y-3 rounded bg-bg-2 p-4">
          <p className="text-xs text-text-mute">{t("以下设置属于")}{modelName(model.id)}{t("，会应用到所有使用它的角色。")}</p>
          <label className="block text-sm"><input type="checkbox" checked={model.reasoning_supported} onChange={e => updateModel(index, { reasoning_supported: e.target.checked })} /> {t("发送 reasoning 参数（模型需支持）")}</label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm">{t("推理强度")}<select disabled={!model.reasoning_supported} className={input} value={model.reasoning_effort} onChange={e => updateModel(index, { reasoning_effort: e.target.value })}>
              {["", "minimal", "low", "medium", "high", "xhigh"].map(v => <option key={v} value={v}>{v || t("服务默认")}</option>)}</select></label>
            <label className="text-sm">{t("推理摘要")}<select disabled={!model.reasoning_supported} className={input} value={model.reasoning_summary} onChange={e => updateModel(index, { reasoning_summary: e.target.value })}>
              {["", "auto", "concise", "detailed"].map(v => <option key={v} value={v}>{v || t("服务默认")}</option>)}</select></label>
          </div>
          <label className="block text-sm"><input type="checkbox" checked={model.stream} onChange={e => updateModel(index, { stream: e.target.checked })} /> {t("流式响应")}</label>
          <div className="grid gap-3 sm:grid-cols-2">{([
            ["history_tokens", t("模型历史长度（tokens）")], ["max_input_tokens", t("最大输入长度（tokens）")],
            ...(mode === "api" ? [["max_tokens", t("最大输出长度（tokens）")]] : []),
          ] as Array<["history_tokens" | "max_input_tokens" | "max_tokens", string]>).map(([key, label]) => <label key={key} className="text-sm">{t(label)}<input className={input} type="number" min={1} max={1000000} placeholder={t("留空使用默认")} value={model[key] ?? ""} onChange={e => updateModel(index, { [key]: e.target.value === "" ? null : Number(e.target.value) })} /></label>)}</div>
          <label className="block text-sm">{t("工具调用策略")}<select className={input} value={model.tool_choice} onChange={e => updateModel(index, { tool_choice: e.target.value as "required" | "auto" })}>
            <option value="required">{t("required（推荐）")}</option><option value="auto">{t("auto（模型兼容选项）")}</option></select></label>
          <label className="block text-sm">{t("显式声明支持的参数（可选，逗号分隔）")}<input className={input} value={model.allowed_openai_params.join(",")} onChange={e => updateModel(index, { allowed_openai_params: e.target.value.split(",").map(v => v.trim()).filter(Boolean) })} /></label>
          {mode === "api" && <label className="block text-sm">{t("服务商 thinking 等额外请求参数（JSON 对象）")}<textarea className={input} rows={3} value={model.extra_body ?? ""} onChange={e => updateModel(index, { extra_body: e.target.value || null })} placeholder={model.extra_body_configured ? t("已保存，留空保留；输入 {} 清除。内容不回显。") : t("按服务商文档填写；留空不发送额外参数")} />
            <span className="text-text-mute">{t("不同服务的 thinking 格式不同。请按实际服务文档配置，勿在此放任务指令。")}</span></label>}
        </div></details>
        </div>
        {profile.models.length > 1 && <button className={button} type="button" disabled={(Object.keys(ROLE_LABELS) as Role[]).some(role => profile[role] === model.id)} onClick={() => changeProfile({ ...profile, models: profile.models.filter((_, i) => i !== index) })}>{t("移除模型（先解除角色选择）")}</button>}
        </div>
      </details>)}
      <button className={button} type="button" onClick={() => {
        setExpandedModels(previous => ({ ...previous, "": true }));
        changeProfile({ ...profile, models: [...profile.models, emptyModel()] });
      }}>{t("添加自定义模型")}</button>
      </div>
      <div hidden={section !== "execution"} className="space-y-5">
      {runtime && <details open className="rounded-xl border border-border p-5"><summary className="cursor-pointer text-sm font-semibold">{t("任务与 Skill 学习默认预算")}</summary>
        <p className="mt-3 text-sm text-text-mute">{t("Skill 学习（自进化）会探索 App 的操作方法，生成待评审的可复用技能。这些预算限制每次学习的开销；保存预算不会启动学习。")}</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="text-sm">{t("任务流程")}<select className={input} value={runtime.agent_architecture} onChange={e => setRuntime({ ...runtime, agent_architecture: e.target.value as RuntimeSettings["agent_architecture"] })}><option value="plan_executor">{t("Planner + Executor（默认）")}</option><option value="plan_reviewer">Planner + Executor + Reviewer</option></select></label>
        {numbers.map(({ key, label, min, max, optional }) => <label key={key} className="text-sm">{t(label)}<input className={input} type="number" required={!optional} min={min} max={max} value={runtime[key] ?? ""} onChange={e => setRuntime({ ...runtime, [key]: e.target.value === "" ? null : Number(e.target.value) })} /></label>)}
      </div><p className="mt-3 text-sm text-text-mute">{t("时限从创建任务开始计算，暂停不会延长。Skill 学习仍需每次确认模型开销和设备操作；结果需评审与验证，不会自动发布。")}</p></details>}
      <JevSettingsPanel value={jev} onChange={setJev} />
      </div>
      <div className="flex items-center justify-between gap-4 border-t border-border pt-5">
        <span className="text-xs text-text-mute">{t(Object.keys(customRoleNames).length ? "请先确认或取消自定义模型" : "保存后用于新任务")}</span>
        <button className="setup-button setup-button-primary inline-flex items-center gap-2" type="button" disabled={!profile.default_model || Object.keys(customRoleNames).length > 0} onClick={() => perform(save)}><Save size={15} />{busy ? t("保存中…") : t("保存设置")}</button>
      </div>
    </fieldset>
    {preferences && <details hidden={section !== "execution"} className="rounded-xl border border-border p-5"><summary className="cursor-pointer text-sm">{t("本地任务统计与报告导出")}</summary><div className="mt-3 space-y-3 text-sm">
      <label className="block"><input type="checkbox" checked={preferences.local_recording} disabled={busy} onChange={e => perform(async () => { const value = await updateLearningPreferences({ ...preferences, local_recording: e.target.checked }); setPreferences(value); qc.setQueryData(["learning-preferences"], value); })} /> {t("记录任务统计与 Skill 改进线索（仅保存在本机）")}</label>
      <p className="text-text-mute">{t("任务结束后记录成功/失败、重复动作等线索，供后续 Skill 学习参考。记录本身不会额外调用模型或操作手机。")}</p>
      <label className="block"><input type="checkbox" checked={preferences.contribution_enabled} disabled={busy} onChange={e => perform(async () => { const value = await updateLearningPreferences({ ...preferences, contribution_enabled: e.target.checked }); setPreferences(value); qc.setQueryData(["learning-preferences"], value); })} /> {t("允许在任务详情中手动导出统计报告（默认关闭）")}</label>
      <p className="text-text-mute">{t("报告可用于反馈问题，包含 App 包名、结果和动作计数，不含截图、任务指令、输入内容或设备标识。开启后可预览和下载，不会自动上传。")}</p>
    </div></details>}
  </section>;
}
