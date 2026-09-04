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
import { MirrorPanel } from "@/components/MirrorPanel";
import { TaskSkillLinks } from "@/components/TaskSkillLinks";
import { adbSerialFromKey, cancelTask, learnFromTask } from "@/api/client";
import { useTaskTimeline } from "@/state/useTaskTimeline";
import {
  agentCallHash,
  callKeyForAgentHash,
  resolveRoleCalls,
} from "@/lib/expandRoleCalls";
import type { RoleCall } from "@/api/types";

export function TaskDetailView() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { timeline, task } = useTaskTimeline(id);
  const [selectedCallKey, setSelectedCallKey] = useState<string | null>(null);
  const [followLive, setFollowLive] = useState(true);
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
    !readOnly && (tk?.status === "running" || tk?.status === "queued");

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

  const learnMutation = useMutation({
    mutationFn: () => learnFromTask(id!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });

  useEffect(() => {
    if (!running) setStopping(false);
  }, [running]);

  useEffect(() => {
    const syncFromHash = (forceScroll: boolean) => {
      const callKey = callKeyForAgentHash(calls, window.location.hash);
      if (callKey == null) return;
      setSelectedCallKey(callKey);
      setFollowLive(false);
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
    setSelectedCallKey(callKey);
    setFollowLive(false);
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

  const changeFollowLive = (enabled: boolean) => {
    setFollowLive(enabled);
    if (!enabled) return;

    // Re-entering live mode explicitly leaves any historical deep-link
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
  useEffect(() => {
    if (!running || !followLive) return;
    if (defaultCallKey != null && defaultCallKey !== selectedCallKey) {
      setSelectedCallKey(defaultCallKey);
      navigatedCallKeyRef.current = defaultCallKey;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, followLive, defaultCallKey]);

  if (timeline.isLoading || !tl) {
    return <div className="font-mono text-xs text-text-mute">loading task…</div>;
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
            {tk.status === "cancelled"
              ? "已取消"
              : tk.status === "failed"
                ? "运行时失败"
                : tk.status === "succeeded"
                  ? "运行时完成"
                  : tk.status === "running"
                    ? "运行中"
                    : tk.status === "queued"
                      ? "排队"
                      : tk.status}
          </Badge>
        )}
        {readOnly && (
          <Badge variant="outline" title={tk?.data_source || tl?.data_source}>
            temp · read only
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
        {!running && !readOnly && id && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="ml-auto font-mono text-[10px]"
            disabled={learnMutation.isPending}
            onClick={() => learnMutation.mutate()}
          >
            {learnMutation.isPending
              ? "learning…"
              : learnMutation.isSuccess
                ? "learn queued"
                : "learn from this task"}
          </Button>
        )}
        {running && (
          <Button
            type="button"
            variant="destructive"
            size="sm"
            className="ml-auto font-mono text-[10px]"
            disabled={stopping || cancelMutation.isPending || !id}
            onClick={() => {
              if (!window.confirm("确认强制终止该任务？")) return;
              cancelMutation.mutate();
            }}
          >
            <Square className="mr-1 h-3 w-3 fill-current" />
            {stopping || cancelMutation.isPending ? "正在停止…" : "强制终止"}
          </Button>
        )}
      </div>

      <TaskHeader
        timeline={tl}
        executionElapsedMs={tk?.execution_elapsed_ms}
        liveTask={tk}
      />

      {id ? <TaskSkillLinks taskId={id} /> : null}

      <Tabs defaultValue="timeline">
        <TabsList>
          <TabsTrigger value="timeline">Timeline</TabsTrigger>
          <TabsTrigger value="traces">Trace 流</TabsTrigger>
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
                  followLive={followLive}
                  onFollowLiveChange={changeFollowLive}
                />
              </CardContent>
            </Card>
            <Card className="min-w-0">
              <CardContent className="p-4">
                {selectedCall ? (
                  <StepInspector
                    call={selectedCall}
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
                  <MirrorPanel
                    taskId={id}
                    deviceKey={tk?.device_serial || tl.device_serial || null}
                    deviceSerial={
                      adbSerialFromKey(tk?.device_serial || tl.device_serial) ||
                      null
                    }
                    selectedStepId={
                      selectedCall?.step_seq != null
                        ? selectedCall.step_seq
                        : null
                    }
                    selectedSomRef={
                      selectedCall?.observation?.som_ref ?? null
                    }
                    action={
                      selectedCall?.role === "executor"
                        ? (selectedCall.executor?.action ?? null)
                        : null
                    }
                    frameGeometry={
                      selectedCall?.observation?.frame_geometry ?? null
                    }
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
      </Tabs>
    </div>
  );
}
