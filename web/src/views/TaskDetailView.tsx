import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, Download, Smartphone, Square } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Timeline } from "@/components/Timeline";
import { TraceStream } from "@/components/TraceStream";
import { StepInspector } from "@/components/StepInspector";
import { TaskHeader } from "@/components/TaskHeader";
import { ObservationPanel } from "@/components/ObservationPanel";
import { resolveObservationImage } from "@/lib/observationImage";
import { TaskSkillLinks } from "@/components/TaskSkillLinks";
import { SkillLearningPanel } from "@/components/SkillLearningPanel";
import { LearningControls } from "@/components/LearningControls";
import { JevChecksPanel } from "@/components/JevChecksPanel";
import { approveScriptGeneration, cancelTask, getScriptGeneration, pauseTask, resumeTask } from "@/api/client";
import { taskCanPause, taskCanResume, taskIsActive, taskIsNonterminal, taskStatusLabel } from "@/lib/taskLifecycle";
import { useTaskTimeline } from "@/state/useTaskTimeline";
import {
  agentCallHash,
  callKeyForAgentHash,
  resolveRoleCalls,
} from "@/lib/expandRoleCalls";
import type { ConversationVisual } from "@/lib/agentCalls";
import type { RoleCall } from "@/api/types";

type SelectedConversationVisual = ConversationVisual & {
  callKey: string | null;
};

const scriptMessages = {
  waiting: "等待生成脚本",
  queued: "等待生成脚本",
  running: "正在生成录制脚本…",
  verifying: "正在专用模拟器独立回放脚本…",
  draft: "脚本草稿已生成，等待回放验证",
  blocked: "未能从轨迹生成完整脚本",
  error: "脚本生成失败",
};

const scriptVerificationMessages: Record<string, string> = {
  automatically_registered: "两次独立回放通过，脚本已自动登记",
  approved: "脚本已批准登记",
  verified_pending_review: "两次独立回放通过，脚本待发布审批",
  validation_incomplete: "脚本草稿已生成，独立复验未通过",
  automatic_enrollment_deferred: "脚本草稿已生成，登记条件未满足",
  blocked: "脚本草稿已生成，暂不能复验",
  error: "脚本草稿已生成，复验失败",
};

export function TaskDetailView() {
  useLocale();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { timeline, task } = useTaskTimeline(id);
  const [selectedCallKey, setSelectedCallKey] = useState<string | null>(null);
  const [selectedConversationVisual, setSelectedConversationVisual] =
    useState<SelectedConversationVisual | null>(null);
  const [followLatest, setFollowLatest] = useState(true);
  const [stopping, setStopping] = useState(false);
  const budgetRenewal = useRef<{ request_key: string; budget: Record<string, number> } | null>(null);
  // Last call key we already navigated to (rail click or hash scroll).
  // Silent `calls` merges must not re-scroll while this matches.
  const navigatedCallKeyRef = useRef<string | null>(null);

  const tl = timeline.data;
  const tk = task.data;

  const calls: RoleCall[] = useMemo(() => {
    return resolveRoleCalls(tl?.steps ?? [], tl?.calls);
  }, [tl]);
  const readOnly = Boolean(tk?.read_only || tl?.read_only);
  const showScript = Boolean(id && !readOnly && tk?.generate_test_script && tk.status === "succeeded");
  const script = useQuery({
    queryKey: ["script-generation", id],
    queryFn: () => getScriptGeneration(id!),
    enabled: showScript,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "waiting" || status === "queued" || status === "running" || status === "verifying" ? 1500 : false;
    },
  });
  const scriptApproval = useMutation({
    mutationFn: () => approveScriptGeneration(id!),
    onSuccess: (result) => qc.setQueryData(["script-generation", id], result),
  });
  const running =
    !readOnly && taskIsActive(tk?.status);
  const canStop = !readOnly && taskIsNonterminal(tk?.status);
  const lifecycleMutation = useMutation({
    mutationFn: async (operation: "pause" | "resume") => {
      if (operation === "pause") return pauseTask(id!);
      const runtime = tk?.state?.revisable;
      if (runtime?.stop_reason !== "budget_exhausted") return resumeTask(id!);
      const dimension = runtime.budget_kind === "task_time" ? "seconds" : runtime.budget_kind;
      if (!dimension) throw new Error("缺少预算类型，请查看任务详情");
      const label = ({ executor_decisions: "执行决策次数", model_calls: "模型请求次数", device_actions: "设备动作次数", seconds: "时间（秒）" } as Record<string, string>)[dimension] ?? dimension;
      const amount = window.prompt(`追加${label}预算（正整数；增加累计上限，不重置消耗）`,
        dimension === "model_calls" ? "1000" : dimension === "seconds" ? "1800" : "200");
      if (amount === null) return { task_id: id!, status: tk?.status };
      const increment = Number(amount);
      if (!Number.isSafeInteger(increment) || increment <= 0) throw new Error("请输入正整数");
      const budget = { [dimension]: increment };
      if (!budgetRenewal.current || JSON.stringify(budgetRenewal.current.budget) !== JSON.stringify(budget)) {
        budgetRenewal.current = { request_key: crypto.randomUUID(), budget };
      }
      return resumeTask(id!, budgetRenewal.current);
    },
    onSuccess: () => {
      budgetRenewal.current = null;
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["timeline", id] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelTask(id!),
    onSuccess: (body) => {
      setStopping(!body.already_terminal && body.status !== "cancelled");
      qc.invalidateQueries({ queryKey: ["task", id] });
      qc.invalidateQueries({ queryKey: ["timeline", id] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  useEffect(() => {
    if (!canStop) setStopping(false);
  }, [canStop]);

  useEffect(() => {
    const syncFromHash = (forceScroll: boolean) => {
      const callKey = callKeyForAgentHash(calls, window.location.hash);
      if (callKey == null) return;
      setSelectedCallKey(callKey);
      setFollowLatest(false);
      if (!forceScroll && navigatedCallKeyRef.current === callKey) return;
      navigatedCallKeyRef.current = callKey;
      window.requestAnimationFrame(() => {
        document.getElementById(window.location.hash.slice(1))?.scrollIntoView({
          block: "start",
        });
      });
    };
    // Selection sync on every calls update; scroll only on new call key
    // (or hashchange via forceScroll).
    syncFromHash(false);
    const onHashChange = () => syncFromHash(true);
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [calls]);

  const selectCall = (callKey: string) => {
    setSelectedConversationVisual(null);
    setSelectedCallKey(callKey);
    setFollowLatest(false);
    // Rail click already places the inspector; mark navigated so the next
    // SSE-driven `calls` sync does not steal scroll back to the anchor.
    navigatedCallKeyRef.current = callKey;
    const call = calls.find((candidate) => candidate.call_key === callKey);
    const hash = call ? agentCallHash(call) : null;
    if (hash && hash !== window.location.hash) {
      window.history.replaceState(
        window.history.state,
        "",
        `${window.location.pathname}${window.location.search}${hash}`,
      );
    }
  };

  // Default selection: latest role call (prefer latest overall).
  const defaultCallKey = useMemo(() => {
    if (calls.length === 0) return null;
    return calls[calls.length - 1].call_key;
  }, [calls]);

  const changeFollowLatest = (enabled: boolean) => {
    setFollowLatest(enabled);
    if (!enabled) return;
    setSelectedConversationVisual(null);

    // Re-entering follow mode explicitly leaves any historical deep-link
    // selection behind. Otherwise the calls-driven hash effect would see
    // the old anchor again on the next update and immediately force OFF.
    setSelectedCallKey(defaultCallKey);
    navigatedCallKeyRef.current = defaultCallKey;
    if (window.location.hash) {
      window.history.replaceState(
        window.history.state,
        "",
        `${window.location.pathname}${window.location.search}`,
      );
    }
  };

  const effectiveCallKey = selectedCallKey ?? defaultCallKey;
  const selectedCall =
    calls.find((c) => c.call_key === effectiveCallKey) ?? null;
  const activeConversationVisual = selectedConversationVisual?.callKey === effectiveCallKey
    ? selectedConversationVisual
    : null;
  const selectConversationVisual = (visual: ConversationVisual) => {
    setFollowLatest(false);
    setSelectedConversationVisual({
      ...visual,
      callKey: effectiveCallKey,
    });
  };
  useEffect(() => {
    setSelectedConversationVisual(null);
  }, [effectiveCallKey]);
  useEffect(() => {
    if (!running || !followLatest) return;
    if (defaultCallKey != null && defaultCallKey !== selectedCallKey) {
      setSelectedCallKey(defaultCallKey);
      navigatedCallKeyRef.current = defaultCallKey;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, followLatest, defaultCallKey]);

  if (timeline.isLoading && !tl) {
    return <div className="font-mono text-xs text-text-mute">loading task…</div>;
  }

  if (!tl) {
    return <div className="font-mono text-xs text-text-mute">task not found</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
          <ChevronLeft className="h-4 w-4" /> back
        </Button>
        <span className="font-mono text-xs text-text-mute">
          {tl.task_id.slice(0, 12)}
        </span>
        {tk?.status && (
          <Badge variant="outline">
            {t(taskStatusLabel[tk.status])}
          </Badge>
        )}
        {readOnly && (
          <Badge variant="outline" title={tk?.data_source || tl?.data_source}>
            {(tk?.data_source || tl?.data_source || "eval").split("/")[0]} · read only
          </Badge>
        )}
        {(tk?.device_serial || tl.device_serial) && (
          <span
            className="font-mono text-[10px] text-cyan"
            title={tk?.device_serial || tl.device_serial || undefined}
          >
            {tk?.device_serial || tl.device_serial}
          </span>
        )}
        {running && (
          <span className="font-mono text-[10px] text-neon animate-pulse">
            ● live
          </span>
        )}
        {!readOnly && (
          <Button asChild variant="outline" size="sm">
            <Link to={`/workbench/${encodeURIComponent(tl.task_id)}`}>{t("任务工作台")}</Link>
          </Button>
        )}
        {!readOnly && (
          <Button asChild variant="outline" size="sm">
            <Link to={`/tasks/${encodeURIComponent(tl.task_id)}/preview`} title={t("查看任务投屏与模型输出")}>
              <Smartphone className="mr-1.5 h-4 w-4" aria-hidden />
              {t("投屏预览")}
            </Link>
          </Button>
        )}
        {(taskCanPause(tk?.status, readOnly) || taskCanResume(tk?.status, readOnly, tk?.can_resume)) && (
          <Button type="button" variant="outline" size="sm"
            disabled={lifecycleMutation.isPending || stopping || cancelMutation.isPending}
            onClick={() => lifecycleMutation.mutate(taskCanResume(tk?.status, readOnly, tk?.can_resume) ? "resume" : "pause")}
          >
            {lifecycleMutation.isPending ? t("处理中…") : taskCanResume(tk?.status, readOnly, tk?.can_resume) ? t("恢复任务") : t("暂停任务")}
          </Button>
        )}
        {canStop && (
          <Button
            type="button"
            variant="destructive"
            size="sm"
            className="ml-auto font-mono text-[10px]"
            disabled={stopping || cancelMutation.isPending || !id}
            onClick={() => {
              if (!window.confirm(t("确认强制终止该任务？"))) return;
              cancelMutation.mutate();
            }}
          >
            <Square className="mr-1 h-3 w-3 fill-current" />
            {stopping || cancelMutation.isPending ? t("正在停止…") : t("强制终止")}
          </Button>
        )}
      </div>

      {(lifecycleMutation.error || cancelMutation.error) && (
        <div role="alert" className="text-sm text-err">
          {String(lifecycleMutation.error || cancelMutation.error)}
        </div>
      )}

      <TaskHeader
        timeline={tl}
        executionElapsedMs={tk?.execution_elapsed_ms}
        liveTask={tk}
      />

      {showScript && (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-border px-3 py-2 text-xs">
          <span className="text-text-mute" role="status">
            {t(script.isError ? "无法读取录制脚本状态" : script.data
              ? scriptVerificationMessages[script.data.verification_status ?? ""] ?? scriptMessages[script.data.status]
              : script.isPending ? "正在读取录制脚本状态…" : "此任务暂无脚本草稿")}
            {script.data?.reason && (script.data.status === "blocked" || script.data.status === "error") && (
              <span className="ml-2 font-mono">{script.data.reason}</span>
            )}
          </span>
          {script.data?.bundle_ref && (
            <Button asChild variant="outline" size="sm">
              <a href={`/api/tasks/${encodeURIComponent(id!)}/script-generation/download`}>
                <Download className="mr-1.5 h-4 w-4" aria-hidden />
                {t("下载脚本草稿")}
              </a>
            </Button>
          )}
          {script.data?.verification_status === "verified_pending_review" && (
            <Button variant="outline" size="sm" onClick={() => scriptApproval.mutate()} disabled={scriptApproval.isPending}>
              {t("批准登记脚本")}
            </Button>
          )}
          {scriptApproval.isError && <span role="alert">{t("脚本登记失败，请核对复验结果")}</span>}
        </div>
      )}

      {id ? <TaskSkillLinks taskId={id} /> : null}
      {id && !canStop && !readOnly ? <LearningControls key={id} taskId={id} /> : null}

      <Tabs defaultValue="timeline">
        <TabsList>
          <TabsTrigger value="timeline">Timeline</TabsTrigger>
          <TabsTrigger value="traces">{t("Trace 流")}</TabsTrigger>
          <TabsTrigger value="jev">{t("Jev 摘要检查")}</TabsTrigger>
          <TabsTrigger value="learning">{t("Skill 学习")}</TabsTrigger>
        </TabsList>
        <TabsContent value="timeline">
          <div className="grid gap-4 lg:grid-cols-[160px_1fr] xl:grid-cols-[160px_1fr_420px]">
            <Card className="min-w-0 overflow-hidden lg:sticky lg:top-20 lg:self-start lg:max-h-[calc(100vh-9rem)]">
              <CardContent className="p-0">
                <Timeline
                  calls={calls}
                  selectedCallKey={effectiveCallKey}
                  onSelectCall={selectCall}
                  running={running}
                  followLatest={followLatest}
                  onFollowLatestChange={changeFollowLatest}
                />
              </CardContent>
            </Card>
            <Card className="min-w-0">
              <CardContent className="p-4">
                {selectedCall ? (
                  <StepInspector
                    call={selectedCall}
                    taskId={id!}
                    selectedVisualKey={activeConversationVisual?.rowKey ?? null}
                    onSelectVisual={selectConversationVisual}
                  />
                ) : (
                  <div className="font-mono text-xs text-text-mute">
                    select a call to inspect
                  </div>
                )}
              </CardContent>
            </Card>
            <div className="hidden lg:block lg:col-span-2 xl:col-span-1">
              <div className="lg:max-w-none xl:sticky xl:top-20 xl:self-start">
                {id ? (
                  <ObservationPanel
                    taskId={id}
                    image={resolveObservationImage(calls, selectedCall, followLatest, activeConversationVisual)}
                  />
                ) : null}
              </div>
            </div>
          </div>
        </TabsContent>
        <TabsContent value="traces">
          <Card>
            <CardContent className="p-4">
              {id ? <TraceStream taskId={id} /> : null}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="learning">
          {id ? <SkillLearningPanel key={id} taskId={id} /> : null}
        </TabsContent>
        <TabsContent value="jev">
          <Card><CardContent className="p-4">
            {id ? <JevChecksPanel taskId={id} running={running} /> : null}
          </CardContent></Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
