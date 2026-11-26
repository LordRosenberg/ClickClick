import { useEffect, useRef, useState, type FormEvent, type ReactNode, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowUp, ChevronDown, ListChecks, Pause, Play, Plus } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  cancelTask, createTask, deviceLabel, getModelCatalog, getWorkbenchTask,
  listDevices, listWorkbenchTasks, pauseTask, resumeTask, steerTask, type WorkbenchTask,
} from "@/api/client";
import { HumanInputInbox } from "@/components/HumanInputInbox";
import { WorkbenchFeed } from "@/components/WorkbenchFeed";
import { WorkbenchLayout } from "@/components/WorkbenchLayout";
import { t } from "@/lib/locale";
import { taskCanPause, taskCanResume, taskIsNonterminal, taskStatusLabel } from "@/lib/taskLifecycle";
import { useLocale } from "@/lib/useLocale";
import { useWorkbenchFeed } from "@/state/useWorkbenchFeed";
import "./workbench.css";

function useRequestKey() {
  const record = useRef<{ signature: string; key: string }>();
  return {
    forRequest(signature: string) {
      if (record.current?.signature !== signature) {
        record.current = { signature, key: Array.from(crypto.getRandomValues(new Uint32Array(4)),
          part => part.toString(16).padStart(8, "0")).join("") };
      }
      return record.current.key;
    },
    reset() { record.current = undefined; },
  };
}

function useWorkbenchRefresh() {
  const cache = useQueryClient();
  return () => {
    for (const key of ["workbench-task", "workbench-tasks", "tasks", "devices", "human-input"]) {
      void cache.invalidateQueries({ queryKey: [key] });
    }
  };
}

function WorkbenchHeader({ task, children }: { task?: WorkbenchTask; children?: ReactNode }) {
  const [switching, setSwitching] = useState(false);
  const switcher = useRef<HTMLDetailsElement>(null);
  const active = useQuery({ queryKey: ["workbench-tasks"], queryFn: listWorkbenchTasks, enabled: switching,
    refetchInterval: switching ? 5000 : false });
  return <header className="wb-header">
    <div className="wb-task-title">
      <Link className="wb-button wb-icon" to="/" title={t("任务历史")} aria-label={t("任务历史")}><ArrowLeft size={17} /></Link>
      <details ref={switcher} className="wb-popover" onToggle={event => setSwitching(event.currentTarget.open)}>
        <summary className="wb-title-summary" title={task?.instruction}>
          <h1 className="truncate text-base font-semibold">{task?.instruction.split("\n")[0] || t("新任务")}</h1>
          <ChevronDown size={15} aria-hidden />
        </summary>
        <div className="wb-popup">
          <Link className="wb-menu-item" to="/workbench" onClick={() => { if (switcher.current) switcher.current.open = false; }}>
            <Plus size={15} aria-hidden />{t("新任务")}
          </Link>
          {active.isPending && <p className="wb-menu-item text-text-mute">{t("正在加载任务")}</p>}
          {active.isError && <p className="wb-menu-item text-err">{t("无法加载任务")}</p>}
          {active.data?.map(item => <Link key={item.id} className="wb-menu-item"
            to={`/workbench/${encodeURIComponent(item.id)}`} onClick={() => { if (switcher.current) switcher.current.open = false; }}>
            <span className="min-w-0 flex-1 truncate">{item.instruction}</span>
            <span className="shrink-0 text-xs text-text-mute">{t(taskStatusLabel[item.status])}</span>
          </Link>)}
        </div>
      </details>
      {task && <span className="wb-task-status" data-status={task.status} role="status">{t(taskStatusLabel[task.status])}</span>}
    </div>
    <div className="wb-header-actions">{children}</div>
  </header>;
}

function Composer({ text, onChange, onSubmit, label, placeholder, enabled, busy, error, input, children }: {
  text: string; onChange: (next: string) => void; onSubmit: () => void; label: string; placeholder: string;
  enabled: boolean; busy: boolean; error?: string; input?: RefObject<HTMLTextAreaElement>; children?: ReactNode;
}) {
  const canSend = enabled && !busy && Boolean(text.trim());
  function submit(event: FormEvent) {
    event.preventDefault();
    if (canSend) onSubmit();
  }
  return <form className="wb-compose-form" onSubmit={submit}>
    {error && <p className="wb-form-error" role="alert">{error}</p>}
    <div className="wb-input-card">
      <textarea ref={input} value={text} onChange={event => onChange(event.target.value)}
        placeholder={placeholder} aria-label={placeholder} maxLength={20000} rows={2} disabled={busy}
        onKeyDown={event => {
          if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (!canSend) return;
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }} />
      <div className="wb-input-actions">
        <div className="wb-input-options">{children}</div>
        <button type="submit" className="wb-send" disabled={!canSend}>
          {t(busy ? "正在提交" : label)}<ArrowUp size={16} aria-hidden />
        </button>
      </div>
    </div>
  </form>;
}

function WorkbenchDraft() {
  const navigate = useNavigate();
  const refresh = useWorkbenchRefresh();
  const requestKey = useRequestKey();
  const [instruction, setInstruction] = useState("");
  const [device, setDevice] = useState("");
  const [decision, setDecision] = useState("");
  const [executor, setExecutor] = useState("");
  const [recordScript, setRecordScript] = useState(false);
  const models = useQuery({ queryKey: ["models"], queryFn: getModelCatalog });
  const devices = useQuery({ queryKey: ["devices"], queryFn: listDevices, refetchInterval: 5000 });
  const available = devices.data?.filter(item => item.state === "device" && !item.busy) ?? [];
  const selected = devices.data?.find(item => item.key === device);
  const selectedDevice = device ? selected : (available.length === 1 ? available[0] : undefined);
  const modelChoices = models.data?.models ?? [];
  const modelReady = modelChoices.length > 0;
  const label = selectedDevice && deviceLabel(selectedDevice);
  const create = useMutation({
    mutationFn: () => {
      if (!selectedDevice) throw new Error(t("请选择可用设备"));
      const request = {
        decision_model: decision || models.data?.roles.planner,
        executor_model: executor || models.data?.roles.executor,
        hitl_enabled: true,
        generate_test_script: recordScript,
      };
      const input = instruction.trim();
      const key = requestKey.forRequest(JSON.stringify([input, selectedDevice.key, request]));
      return createTask(input, [selectedDevice.key], { ...request, request_key: key });
    },
    onSuccess: () => { requestKey.reset(); refresh(); },
  });
  const error = create.error?.message || (devices.isError ? t("无法加载设备") : models.isError ? t("无法加载模型") : undefined);
  const composer = <Composer text={instruction} onChange={setInstruction}
    enabled={Boolean(selectedDevice?.state === "device" && !selectedDevice.busy && modelReady)} busy={create.isPending}
    label="开始执行" placeholder={t("描述你想在设备上完成的任务")} error={error}
    onSubmit={() => create.mutate(undefined, { onSuccess: response => {
      if (response.tasks[0]) navigate(`/workbench/${encodeURIComponent(response.tasks[0].id)}`, { replace: true });
    } })}>
    <select className="wb-select" aria-label={t("选择设备")} value={selectedDevice?.key ?? device} disabled={create.isPending}
      onChange={event => setDevice(event.target.value)}>
      <option value="">{t(devices.isPending ? "正在加载设备" : "选择设备")}</option>
      {device && !selected && <option value={device} disabled>{device} · {t("离线")}</option>}
      {devices.data?.map(item => <option key={item.key} value={item.key} disabled={item.busy || item.state !== "device"}>
        {deviceLabel(item)}{item.platform ? ` · ${item.platform}` : ""}
        {item.busy ? ` · ${t("占用中")}` : item.state !== "device" ? ` · ${t("离线")}` : ""}
      </option>)}
    </select>
    <details className="wb-popover wb-model-menu">
      <summary className="wb-button">{t("模型")} · {t(decision || executor ? "本次自定义" : "默认配置")}<ChevronDown size={13} aria-hidden /></summary>
      <div className="wb-popup wb-model-popup">
        {([
          [t("规划与审查"), decision, setDecision, models.data?.roles.planner],
          [t("执行模型"), executor, setExecutor, models.data?.roles.executor],
        ] as const).map(([name, current, choose, standard]) => <label key={name}>
          <span>{name}</span>
          <select className="wb-select w-full" value={current} onChange={event => choose(event.target.value)}
            disabled={create.isPending || !modelReady}>
            <option value="">{t("默认配置")}{standard ? ` · ${standard}` : ""}</option>
            {modelChoices.map(model => <option value={model.id} key={model.id}>{model.id}</option>)}
          </select>
        </label>)}
        <Link to="/setup" className="text-xs text-cyan">{t("查看全局模型设置")}</Link>
      </div>
    </details>
    <label className="wb-button wb-record-script" title={t("任务成功后生成脚本。配置专用模拟器后会自动复验。")}>
      <input type="checkbox" className="accent-neon" checked={recordScript} disabled={create.isPending}
        onChange={event => setRecordScript(event.target.checked)} />
      {t("录制脚本")}
    </label>
    {devices.isSuccess && !available.length && <Link className="text-xs text-text-mute" to="/device">{t("暂无空闲设备")}</Link>}
    {models.isSuccess && !modelReady && <Link className="text-xs text-cyan" to="/setup">{t("配置模型")}</Link>}
  </Composer>;
  return <>
    <WorkbenchHeader />
    <WorkbenchLayout device={selectedDevice?.platform || label} composer={composer} execution={
      <div className="wb-start"><div className="wb-start-icon"><ListChecks size={24} /></div>
        <h2 className="text-xl font-semibold">{t("派发任务")}</h2>
        <p className="text-sm text-text-mute">{t("在下方输入任务，选择一台设备后开始执行。")}</p>
      </div>
    } />
  </>;
}

function RunningWorkbench({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const cache = useQueryClient();
  const refresh = useWorkbenchRefresh();
  const input = useRef<HTMLTextAreaElement>(null);
  const requestKey = useRequestKey();
  const [instruction, setInstruction] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const queryKey = ["workbench-task", taskId] as const;
  const query = useQuery({ queryKey, queryFn: () => getWorkbenchTask(taskId),
    refetchInterval: context => {
      const status = context.state.data?.status;
      return taskIsNonterminal(status) ? status === "paused" ? 3500 : 1200 : false;
    } });
  const task = query.data;
  const active = taskIsNonterminal(task?.status);
  const finished = !active || Boolean(task?.read_only);
  const { rows, connection } = useWorkbenchFeed(taskId, Boolean(task), finished);
  const devices = useQuery({ queryKey: ["devices"], queryFn: listDevices, staleTime: 5000 });
  const device = devices.data?.find(item => item.key === task?.device_serial);
  const label = device ? deviceLabel(device) : task?.device_serial ?? undefined;
  const screenDevice = device?.platform && label ? `${device.platform} · ${label}` : label;
  const steering = useMutation({
    mutationFn: async () => {
      const text = instruction.trim();
      const result = await steerTask(taskId, text, requestKey.forRequest(text));
      if (result.status === "disabled") throw new Error(result.message);
      return result;
    },
    onSuccess: receipt => {
      requestKey.reset();
      setInstruction("");
      cache.setQueryData<WorkbenchTask>(queryKey, current => {
        if (!current || current.adjustments.some(item => item.revision === receipt.revision)) return current;
        return { ...current, adjustments: [...current.adjustments, receipt] };
      });
      refresh();
    },
  });
  const lifecycle = useMutation({
    mutationFn: (action: "pause" | "resume" | "cancel") => {
      if (action === "pause") return pauseTask(taskId);
      if (action === "resume") return resumeTask(taskId);
      return cancelTask(taskId);
    },
    onSuccess: (result, action) => {
      if (action === "cancel" && "cancel_requested" in result) setCancelling(result.cancel_requested === true);
      refresh();
    },
  });
  useEffect(() => {
    if (!active) setCancelling(false);
  }, [active]);

  if (!task) return <>
    <WorkbenchHeader />
    <div className="wb-start" role={query.isError ? "alert" : "status"}>
      <p>{t(query.isError ? "无法加载任务" : "正在加载任务")}</p>
      {query.isError && <button type="button" className="wb-button" onClick={() => void query.refetch()}>{t("重试")}</button>}
    </div>
  </>;

  const writable = !task.read_only;
  const followUp = task.status === "succeeded";
  const canSteer = writable && task.hitl_enabled && !cancelling &&
    (followUp || ["queued", "running", "pausing", "paused", "waiting_for_input"].includes(task.status));
  const roleModels = { ...task.models };
  for (const row of [...rows.values()].reverse()) {
    if (row.kind !== "thinking" || !row.model) continue;
    if (row.role === "executor") roleModels.executor ??= row.model;
    if (row.role === "planner" || row.role === "reviewer") roleModels.decision ??= row.model;
  }
  const canPause = taskCanPause(task.status, task.read_only);
  const canResume = active && taskCanResume(task.status, task.read_only, task.can_resume);
  const pausing = task.status === "pausing" || (lifecycle.isPending && lifecycle.variables === "pause");
  const resuming = lifecycle.isPending && lifecycle.variables === "resume";
  const elapsed = Math.round((task.execution_elapsed_ms ?? 0) / 1000);
  return <>
    <WorkbenchHeader task={task}>
      <details className="wb-popover wb-task-info">
        <summary className="wb-button">{t("执行配置")}<ChevronDown size={13} aria-hidden /></summary>
        <div className="wb-popup wb-info-popup">
          <div>{t("设备")} <span>{label ?? "–"}</span></div>
          <div>{t("规划与审查")} <span>{roleModels.decision || t("默认配置")}</span></div>
          <div>{t("执行模型")} <span>{roleModels.executor || t("默认配置")}</span></div>
          <div>{t("耗时")} <span>{Math.floor(elapsed / 60)}m {elapsed % 60}s</span></div>
        </div>
      </details>
      {(canPause || canResume || (writable && pausing)) && <button className="wb-button" type="button"
        disabled={pausing || lifecycle.isPending || cancelling} onClick={() => lifecycle.mutate(canResume ? "resume" : "pause")}>
        {canResume ? <Play size={14} aria-hidden /> : <Pause size={14} aria-hidden />}
        {t(pausing ? "正在暂停" : resuming ? "正在恢复" : canResume ? "恢复任务" : "暂停任务")}
      </button>}
      {writable && active && <button className="wb-button wb-cancel" type="button" disabled={lifecycle.isPending || cancelling} onClick={() => {
        if (window.confirm(t("确定要取消这个任务吗？"))) lifecycle.mutate("cancel");
      }}>{t(cancelling ? "正在取消" : "取消任务")}</button>}
      <Link className="wb-button" to={`/tasks/${encodeURIComponent(taskId)}`}>{t("查看详情")}</Link>
    </WorkbenchHeader>
    <WorkbenchLayout task={task} device={screenDevice} execution={
      <WorkbenchFeed task={task} rows={rows} connection={connection}
        question={task.status === "waiting_for_input" && writable
          ? <HumanInputInbox taskId={taskId} onAdjust={() => input.current?.focus()} /> : undefined} />
    } composer={canSteer ? <Composer text={instruction} onChange={setInstruction} enabled={!lifecycle.isPending}
      busy={steering.isPending} input={input} label={followUp ? "追加任务" : "发送指令"}
      placeholder={t(followUp ? "输入指令以创建关联的新任务" : "输入指令以补充或修改当前任务")}
      error={steering.error?.message || lifecycle.error?.message}
      onSubmit={() => steering.mutate(undefined, { onSuccess: receipt => {
        if (receipt.followup_task_id) navigate(`/workbench/${encodeURIComponent(receipt.followup_task_id)}`);
      } })}>
      {task.status === "paused" && <span className="text-xs text-text-mute">{t("修改将在继续执行后处理")}</span>}
    </Composer> : <div className="wb-no-composer">
      {lifecycle.error && <p role="alert" className="wb-form-error">{lifecycle.error.message}</p>}
      {writable && active && !task.hitl_enabled && <p className="text-xs text-text-mute">{t("此任务未开启人工干预")}</p>}
      {!active && <Link className="wb-button" to="/workbench"><Plus size={14} aria-hidden />{t("新任务")}</Link>}
    </div>} />
  </>;
}

export function WorkbenchView() {
  useLocale();
  const { id } = useParams();
  return <div className="wb-root">{id ? <RunningWorkbench key={id} taskId={id} /> : <WorkbenchDraft />}</div>;
}
