import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowDown, Check, ChevronRight, CircleHelp, LoaderCircle, Minus, Pause, Play, X } from "lucide-react";
import { Link } from "react-router-dom";
import type { TaskAdjustment, WorkbenchTask } from "@/api/client";
import { t } from "@/lib/locale";
import { taskStatusLabel } from "@/lib/taskLifecycle";
import type { FeedRow, ThinkingRow, ToolRow } from "@/lib/workbenchFeed";

const toolLabels: Record<string, string> = {
  submit_planner_decision: "提交规划", submit_reviewer_decision: "提交审查",
  submit_executor_step: "提交执行步骤", ask_user: "请求用户回复",
  observe_screen: "查看设备画面", get_current_frame: "读取当前画面",
  retrieve_task_memory: "查找任务记录",
  tap: "点击", tap_xy: "点击", double_tap: "双击", launch: "打开应用", type: "输入文本",
  replace_text: "替换文本", scroll: "滚动", swipe: "滑动", drag: "拖动", long_press: "长按",
  key: "按键", back: "返回", home: "回到主屏幕", sleep: "等待",
};
const roleLabels: Record<string, string> = { planner: "规划", reviewer: "审查", executor: "执行" };

function Thinking({ row, active }: { row: ThinkingRow; active: boolean }) {
  const [expanded, setExpanded] = useState<boolean>();
  useEffect(() => setExpanded(undefined), [active]);
  const open = expanded ?? active;
  const role = roleLabels[row.role];
  const progress = row.text || (row.toolCount
    ? t("正在准备 {value0} 个工具调用", { value0: row.toolCount })
    : active ? t("正在等待模型输出") : null);
  return <div className="wb-thinking">
    <button type="button" className="wb-thinking-title" aria-expanded={open} onClick={() => setExpanded(!open)}>
      {active ? <LoaderCircle size={15} className="motion-safe:animate-spin text-neon" /> :
        <ChevronRight size={15} className={open ? "rotate-90" : ""} />}
      <span>{active ? "Thinking…" : "Thinking"}</span>
      {role && <span className="text-text-mute">· {t(role)}</span>}
    </button>
    {open && <div className="wb-thinking-progress">
      {progress || t(row.phase === "finished" ? "模型调用已完成" : "调用未完成")}
      {active && row.text && <span className="wb-caret" aria-hidden />}
    </div>}
  </div>;
}

function Tool({ row, active }: { row: ToolRow; active: boolean }) {
  const pending = row.status === "running";
  const Icon = pending ? (active ? LoaderCircle : Minus) : row.status === "succeeded" ? Check : X;
  const status = pending ? t(active ? "执行中" : "调用未完成") : t(row.status === "succeeded" ? "成功" : "失败");
  return <div className="wb-tool">
    {row.name && <div className="wb-tool-line">
      <Icon size={15} className={pending && active ? "motion-safe:animate-spin text-cyan" :
        row.status === "failed" ? "text-err" : row.status === "succeeded" ? "text-neon" : "text-text-mute"} aria-hidden />
      <span className="min-w-0 break-all" title={row.name}>
        {toolLabels[row.name] ? t(toolLabels[row.name]) : row.name}
      </span>
      <span className="shrink-0 text-text-mute">{status}{!!row.duration && ` · ${(row.duration / 1000).toFixed(1)}s`}</span>
    </div>}
    {row.text && <p className="wb-tool-text">{row.text}</p>}
  </div>;
}

function UserMessage({ children, adjustment, paused }: { children: ReactNode; adjustment?: TaskAdjustment; paused: boolean }) {
  const receipt = adjustment && ({
    accepted: paused ? "已收到，继续后处理" : "已收到",
    applied: "已应用", cancelled: "未应用", follow_up: "已创建后续任务",
  })[adjustment.status];
  return <div className="wb-user">
    <span className="text-xs font-medium text-text-mute">{t("你")}</span>
    <div className="wb-bubble">{children}</div>
    {receipt && <div className={adjustment?.status === "applied" ? "wb-receipt text-neon" : "wb-receipt"}>
      {t(receipt)}
      {adjustment?.followup_task_id && <Link className="ml-2 text-cyan" to={`/workbench/${encodeURIComponent(adjustment.followup_task_id)}`}>
        {t("打开后续任务")}
      </Link>}
    </div>}
  </div>;
}

const outcomes: Record<string, string> = {
  answered: "你已提交回答", confirmed: "你已确认操作", declined: "你已拒绝操作",
  unable_to_answer: "你标记了无法回答", cancel_task: "你请求取消任务",
};
const lifecycleLabels: Record<string, string> = {
  operator_pause_requested: "已请求暂停", task_paused: "任务已暂停", operator_resume_requested: "已请求恢复",
};

export function WorkbenchFeed({ task, rows, connection, question }: {
  task: WorkbenchTask; rows: Map<string, FeedRow>; connection: string; question?: ReactNode;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [atEnd, setAtEnd] = useState(true);
  useEffect(() => {
    const element = content.current;
    if (!element) return;
    const resize = new ResizeObserver(() => {
      if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    });
    resize.observe(element);
    return () => resize.disconnect();
  }, []);

  const messages = [...rows.values(), ...task.adjustments.map(adjustment =>
    ({ kind: "adjustment" as const, id: `instruction:${adjustment.revision}`, ts: adjustment.created_at, adjustment }))
  ].sort((a, b) => a.ts - b.ts);
  const latestThinking = messages.filter(row => row.kind === "thinking").at(-1);
  const live = task.status === "running" || task.status === "pausing";
  return <>
    <header className="wb-flow-header">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">{t("执行流")}</h2>
        {connection === "retrying" && <span className="text-xs text-amber">{t("正在重连执行流")}</span>}
      </div>
      {task.current_subgoal && <p className="mt-1 truncate text-xs text-text-mute" title={task.current_subgoal}>
        <span className="mr-2">{t("当前目标")}</span>{task.current_subgoal}
      </p>}
    </header>
    <div className="wb-scroll" ref={scroll} tabIndex={0} aria-label={t("任务对话")} onScroll={event => {
      const el = event.currentTarget;
      const bottom = el.scrollHeight - el.clientHeight - el.scrollTop < 40;
      follow.current = bottom;
      setAtEnd(bottom);
    }}>
      <div className="wb-feed" ref={content}>
        <UserMessage paused={false}>{task.instruction}</UserMessage>
        {!messages.length && task.status === "queued" && <p className="wb-tool-text">{t("等待任务开始")}</p>}
        {messages.map(row => {
          if (row.kind === "adjustment") return <UserMessage key={row.id} adjustment={row.adjustment}
            paused={task.status === "paused" || task.status === "pausing"}>{row.adjustment.instruction}</UserMessage>;
          if (row.kind === "thinking") return <Thinking key={row.id} row={row}
            active={live && row.id === latestThinking?.id && row.phase !== "finished"} />;
          if (row.kind === "human") return <div key={row.id} className="wb-user text-xs text-text-mute">{t(outcomes[row.outcome] || "已收到用户回复")}</div>;
          if (row.kind === "lifecycle") {
            const label = lifecycleLabels[row.event];
            if (!label) return null;
            const Icon = row.event === "operator_resume_requested" ? Play : Pause;
            return <div key={row.id} className="wb-tool-line text-xs text-text-mute"><Icon size={14} aria-hidden />{t(label)}</div>;
          }
          return <Tool key={row.id} row={row} active={live && row.ts >= (latestThinking?.ts ?? 0)} />;
        })}
        {question}
        {task.status === "waiting_for_input" && <div className="wb-waiting"><CircleHelp size={15} aria-hidden />{t("等待你回复或修改任务")}</div>}
        {(task.status === "succeeded" || task.status === "failed" || task.status === "cancelled") &&
          <div className="wb-reply">
            <span className="text-xs font-semibold text-neon">ClickClick</span>
            <p className="wb-bubble">{(task.status === "succeeded" ? task.result : task.failure_reason) || t(taskStatusLabel[task.status])}</p>
          </div>}
      </div>
    </div>
    {!atEnd && <button type="button" className="wb-button wb-jump" onClick={() => {
      follow.current = true;
      if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
      setAtEnd(true);
    }}><ArrowDown size={14} aria-hidden />{t("回到最新")}</button>}
  </>;
}
