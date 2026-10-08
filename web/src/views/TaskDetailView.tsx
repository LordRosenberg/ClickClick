import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, Square } from "lucide-react";

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
import { cancelTask, pauseTask, resumeTask } from "@/api/client";
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
  // Last call key we already navigated to (rail click or hash scroll).
  // Silent `calls` merges must not re-scroll while this matches.
  const navigatedCallKeyRef = useRef<string | null>(null);

  const tl = timeline.data;
  const tk = task.data;

  const calls: RoleCall[] = useMemo(() => {
    return resolveRoleCalls(tl?.steps ?? [], tl?.calls);
  }, [tl]);
  const readOnly = Boolean(tk?.read_only || tl?.read_only);
  const running =
    !readOnly && taskIsActive(tk?.status);
  const canStop = !readOnly && taskIsNonterminal(tk?.status);
  const lifecycleMutation = useMutation({
    mutationFn: (operation: "pause" | "resume") => operation === "pause" ? pauseTask(id!) : resumeTask(id!),
    onSuccess: () => {
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
      <div className="flex items-center gap-2">
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
        {(taskCanPause(tk?.status, readOnly) || taskCanResume(tk?.status, readOnly)) && (
          <Button type="button" variant="outline" size="sm"
            disabled={lifecycleMutation.isPending || stopping || cancelMutation.isPending}
            onClick={() => lifecycleMutation.mutate(tk?.status === "paused" ? "resume" : "pause")}
          >
            {lifecycleMutation.isPending ? t("处理中…") : tk?.status === "paused" ? t("恢复任务") : t("暂停任务")}
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

      {id ? <TaskSkillLinks taskId={id} /> : null}
      {id && !canStop && !readOnly ? <LearningControls taskId={id} /> : null}

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
