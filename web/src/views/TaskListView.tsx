import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Send, Square } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  cancelTask,
  createTask,
  DEFAULT_SKILL_LEARN,
  deviceLabel,
  getModelCatalog,
  listDevices,
  listFailedTasks,
  listTasks,
} from "@/api/client";
import { ChatGPTLoginCard } from "@/components/ChatGPTLoginCard";
import { cn, formatMs } from "@/lib/utils";
import type { Task, TaskStatus } from "@/api/types";

const statusVariant: Record<
  TaskStatus,
  "default" | "secondary" | "success" | "destructive" | "neon" | "warn"
> = {
  queued: "secondary",
  running: "neon",
  succeeded: "secondary",
  failed: "destructive",
  cancelled: "warn",
};

const statusLabel: Record<TaskStatus, string> = {
  queued: "排队",
  running: "运行中",
  succeeded: "运行时完成",
  failed: "运行时失败",
  cancelled: "已取消",
};

function statusAccent(status: TaskStatus): string {
  switch (status) {
    case "running":
      return "border-neon/40 bg-neon/[0.06] hover:border-neon/60";
    case "queued":
      return "border-amber/30 hover:border-amber/50";
    case "succeeded":
      return "border-emerald-500/30 hover:border-emerald-500/50";
    case "failed":
      return "border-err/40 hover:border-err/60";
    case "cancelled":
      return "border-amber/30 hover:border-amber/50";
  }
}

function statusDot(status: TaskStatus): string {
  switch (status) {
    case "running":
      return "bg-neon animate-pulse-neon shadow-neon";
    case "queued":
      return "bg-amber";
    case "succeeded":
      return "bg-emerald-500";
    case "failed":
      return "bg-err";
    case "cancelled":
      return "bg-amber";
  }
}

function TaskRow({
  task,
}: {
  task: {
    id: string;
    instruction: string;
    status: TaskStatus;
    step_number?: number;
    failure_reason?: string | null;
    current_subgoal?: string;
    device_serial?: string | null;
    read_only?: boolean;
    data_source?: string;
    execution_elapsed_ms?: number;
  };
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [stopping, setStopping] = useState(false);
  const canStop =
    !task.read_only && (task.status === "running" || task.status === "queued");

  const stopMutation = useMutation({
    mutationFn: () => cancelTask(task.id),
    onSuccess: (body) => {
      setStopping(!body.already_terminal && body.status !== "cancelled");
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  useEffect(() => {
    if (task.status !== "running" && task.status !== "queued") {
      setStopping(false);
    }
  }, [task.status]);

  return (
    <button
      type="button"
      onClick={() => navigate(`/tasks/${task.id}`)}
      className={cn(
        "group w-full rounded border bg-bg-1 p-3 text-left transition-colors",
        statusAccent(task.status),
      )}
    >
      <div className="flex items-center gap-2">
        <span className={cn("inline-block h-2 w-2 rounded-full", statusDot(task.status))} />
        <span className="font-mono text-xs text-text-mute">{task.id.slice(0, 8)}</span>
        <Badge variant={statusVariant[task.status]}>{statusLabel[task.status]}</Badge>
        {task.read_only ? (
          <Badge variant="outline" title={task.data_source}>
            temp
          </Badge>
        ) : null}
        {task.device_serial ? (
          <span
            className="truncate font-mono text-[10px] text-cyan"
            title={task.device_serial}
          >
            {task.device_serial}
          </span>
        ) : null}
        <span className="ml-auto font-mono text-[10px] uppercase tracking-wide text-text-mute">
          step {task.step_number ?? 0}
        </span>
        <span
          className="font-mono text-[10px] uppercase tracking-wide text-cyan tabular-nums"
          title="task wall-clock elapsed"
        >
          elapsed {formatMs(task.execution_elapsed_ms)}
        </span>
        {canStop && (
          <Button
            type="button"
            variant="destructive"
            size="sm"
            className="h-6 px-2 font-mono text-[10px]"
            disabled={stopping || stopMutation.isPending}
            title="强制终止"
            onClick={(e) => {
              e.stopPropagation();
              if (!window.confirm("确认强制终止该任务？")) return;
              stopMutation.mutate();
            }}
          >
            <Square className="mr-1 h-3 w-3 fill-current" />
            {stopping || stopMutation.isPending ? "正在停止…" : "终止"}
          </Button>
        )}
      </div>
      <div className="mt-1 line-clamp-2 font-mono text-[12px] text-text">
        {task.instruction}
      </div>
      {(task.current_subgoal || task.failure_reason) && (
        <div className="mt-1 font-mono text-[10px] uppercase tracking-wide text-text-mute">
          {task.current_subgoal ? (
            <span>
              <span className="text-text-mute/60">subgoal</span>{" "}
              <span className="text-text">{task.current_subgoal}</span>
            </span>
          ) : null}
          {task.failure_reason ? ` · ${task.failure_reason}` : ""}
        </div>
      )}
    </button>
  );
}

function SubmitForm() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [instruction, setInstruction] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [skillLearn, setSkillLearn] = useState(DEFAULT_SKILL_LEARN);
  const [decisionModel, setDecisionModel] = useState("");
  const [executorModel, setExecutorModel] = useState("");
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: listDevices,
    refetchInterval: 3000,
  });
  const catalog = useQuery({
    queryKey: ["models"],
    queryFn: getModelCatalog,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const online = devices.data ?? [];
  const selectable = useMemo(
    () => online.filter((d) => !d.busy),
    [online],
  );
  const modelOptions = catalog.data?.models ?? [];

  // Pre-select the sole online free device.
  useEffect(() => {
    if (selectable.length === 1) {
      setSelected([selectable[0].key]);
    }
  }, [selectable]);

  // Default selectors to resolved role models once catalog loads.
  useEffect(() => {
    if (!catalog.data) return;
    const ids = new Set(catalog.data.models.map((m) => m.id));
    const planner = catalog.data.roles.planner;
    const exe = catalog.data.roles.executor;
    setDecisionModel((prev) => prev || (ids.has(planner) ? planner : catalog.data.models[0]?.id || ""));
    setExecutorModel((prev) => prev || (ids.has(exe) ? exe : catalog.data.models[0]?.id || ""));
  }, [catalog.data]);

  const mutation = useMutation({
    mutationFn: ({
      text,
      serials,
      skill_learn,
      decision_model,
      executor_model,
    }: {
      text: string;
      serials: string[];
      skill_learn: boolean;
      decision_model?: string;
      executor_model?: string;
    }) =>
      createTask(text, serials, {
        skill_learn,
        decision_model,
        executor_model,
      }),
    onSuccess: (data) => {
      setInstruction("");
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["devices"] });
      const tasks = data.tasks ?? [];
      if (tasks.length === 1) {
        navigate(`/tasks/${tasks[0].id}`);
      }
      // Multi fan-out: stay on list so all new rows are visible.
    },
  });

  const toggle = (key: string) => {
    setSelected((prev) =>
      prev.includes(key) ? prev.filter((s) => s !== key) : [...prev, key],
    );
  };

  const canSubmit =
    !!instruction.trim() && selected.length > 0 && !mutation.isPending;

  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <CardTitle className="font-mono text-xs uppercase tracking-wider text-text-mute">
          ▶ submit task
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const text = instruction.trim();
            if (text && selected.length > 0) {
              mutation.mutate({
                text,
                serials: selected,
                skill_learn: skillLearn,
                decision_model: decisionModel || undefined,
                executor_model: executorModel || undefined,
              });
            }
          }}
          className="space-y-3"
        >
          <textarea
            className="min-h-[100px] w-full rounded border border-border bg-bg-2 px-3 py-2 font-mono text-xs text-text placeholder:text-text-mute/60 focus-visible:border-cyan focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan"
            placeholder="输入指令，例如：打开设置并关闭蓝牙"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
          />
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="space-y-1 font-mono text-[11px] text-text-mute">
              <span className="uppercase tracking-wide">planner + reviewer model</span>
              <select
                className="w-full rounded border border-border bg-bg-2 px-2 py-1.5 text-xs text-text focus-visible:border-cyan focus-visible:outline-none"
                value={decisionModel}
                onChange={(e) => setDecisionModel(e.target.value)}
                disabled={modelOptions.length === 0}
              >
                {modelOptions.length === 0 && (
                  <option value="">— configure MODELS_JSON —</option>
                )}
                {modelOptions.map((m) => (
                  <option key={`decision-${m.id}`} value={m.id}>
                    {m.id}
                    {m.is_chatgpt ? " (chatgpt)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1 font-mono text-[11px] text-text-mute">
              <span className="uppercase tracking-wide">executor model</span>
              <select
                className="w-full rounded border border-border bg-bg-2 px-2 py-1.5 text-xs text-text focus-visible:border-cyan focus-visible:outline-none"
                value={executorModel}
                onChange={(e) => setExecutorModel(e.target.value)}
                disabled={modelOptions.length === 0}
              >
                {modelOptions.length === 0 && (
                  <option value="">— configure MODELS_JSON —</option>
                )}
                {modelOptions.map((m) => (
                  <option key={`exe-${m.id}`} value={m.id}>
                    {m.id}
                    {m.is_chatgpt ? " (chatgpt)" : ""}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="flex cursor-pointer items-center gap-2 font-mono text-[11px] text-text-mute">
            <input
              type="checkbox"
              checked={skillLearn}
              onChange={(e) => setSkillLearn(e.target.checked)}
            />
            skill learn (post-task, opt-in)
          </label>
          <div className="space-y-1.5">
            <div className="font-mono text-[10px] uppercase tracking-wide text-text-mute">
              devices {selected.length > 0 ? `(${selected.length})` : ""}
            </div>
            {devices.isLoading && (
              <div className="font-mono text-[10px] text-text-mute">loading devices…</div>
            )}
            {!devices.isLoading && online.length === 0 && (
              <div className="font-mono text-[10px] text-err">
                no online devices — connect a phone via adb
              </div>
            )}
            <div className="max-h-40 space-y-1 overflow-auto rounded border border-border bg-bg-2 p-2">
              {online.map((d) => {
                const checked = selected.includes(d.key);
                return (
                  <label
                    key={d.key}
                    className={cn(
                      "flex cursor-pointer items-start gap-2 rounded px-1 py-1 font-mono text-[11px]",
                      d.busy ? "cursor-not-allowed opacity-50" : "hover:bg-bg-1",
                    )}
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      disabled={d.busy}
                      checked={checked}
                      onChange={() => toggle(d.key)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-text">{deviceLabel(d)}</span>
                      <span className="block truncate text-[10px] text-text-mute">
                        {d.driver_id !== "local" && d.driver_id !== "fixture"
                          ? `${d.driver_id}/`
                          : ""}
                        {d.serial}
                        {d.busy ? " · busy" : ""}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="submit"
              disabled={!canSubmit}
              className="font-mono text-xs uppercase tracking-wider"
            >
              <Send className="mr-1 h-3 w-3" />
              {mutation.isPending
                ? "提交中…"
                : selected.length > 1
                  ? `submit ×${selected.length}`
                  : "submit"}
            </Button>
            {mutation.isError && (
              <span className="font-mono text-[10px] uppercase text-err">
                {(mutation.error as Error)?.message?.slice(0, 80) || "submit failed"}
              </span>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function TaskList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["tasks"],
    queryFn: listTasks,
    refetchInterval: 2000,
  });
  if (isLoading)
    return <div className="font-mono text-xs text-text-mute">loading tasks…</div>;
  if (error)
    return <div className="font-mono text-xs text-err">load failed</div>;
  if (!data || data.length === 0)
    return <div className="font-mono text-xs text-text-mute">— no tasks</div>;
  return (
    <div className="space-y-2">
      {data.map((t: Task) => (
        <TaskRow key={t.id} task={t} />
      ))}
    </div>
  );
}

function FailedList() {
  const { data, isLoading } = useQuery({
    queryKey: ["tasks", "failed"],
    queryFn: listFailedTasks,
  });
  return (
    <div className="space-y-2">
      {isLoading && (
        <div className="font-mono text-xs text-text-mute">loading…</div>
      )}
      {!isLoading && (!data || data.length === 0) && (
        <div className="font-mono text-xs text-text-mute">— no failed tasks</div>
      )}
      {data?.map((t) => (
        <TaskRow key={t.id} task={{ ...t, status: "failed" }} />
      ))}
    </div>
  );
}

export function TaskListView() {
  const [tab, setTab] = useState<"all" | "failed">("all");
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
      <div className="space-y-4">
        <div className="flex items-center gap-1 font-mono text-xs uppercase tracking-wide">
          <button
            type="button"
            onClick={() => setTab("all")}
            className={cn(
              "rounded border px-3 py-1 transition-colors",
              tab === "all"
                ? "border-neon/40 bg-neon/10 text-neon"
                : "border-border bg-transparent text-text-mute hover:border-border-hi hover:text-text",
            )}
          >
            all tasks
          </button>
          <button
            type="button"
            onClick={() => setTab("failed")}
            className={cn(
              "rounded border px-3 py-1 transition-colors",
              tab === "failed"
                ? "border-err/40 bg-err/10 text-err"
                : "border-border bg-transparent text-text-mute hover:border-border-hi hover:text-text",
            )}
          >
            failed only
          </button>
        </div>
        <Separator className="bg-border" />
        {tab === "all" ? <TaskList /> : <FailedList />}
      </div>
      <div className="space-y-3 lg:sticky lg:top-20 lg:self-start">
        <SubmitForm />
        <ChatGPTLoginCard />
      </div>
    </div>
  );
}
