import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Save, Trash2, Check, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  approvePendingSkill,
  createSkill,
  deleteSkill,
  getPendingSkill,
  getSkill,
  listPendingSkills,
  listSkills,
  rejectPendingSkill,
  updateSkill,
} from "@/api/client";
import type { PendingSkill, SkillSummary } from "@/api/types";
import { cn } from "@/lib/utils";

type TabId = "canonical" | "pending";

type EditorState = {
  id: string;
  name: string;
  description: string;
  version: string;
  app: string;
  kind: "generic" | "app_core" | "workflow" | "candidate";
  capability: string;
  tags: string;
  triggers: string;
  body: string;
  isNew: boolean;
};

function emptyEditor(): EditorState {
  return {
    id: "",
    name: "",
    description: "",
    version: "0.1.0",
    app: "",
    kind: "generic",
    capability: "",
    tags: "",
    triggers: "",
    body: "",
    isNew: true,
  };
}

function fromSkill(s: SkillSummary): EditorState {
  return {
    id: s.id,
    name: s.name || s.id,
    description: s.description || "",
    version: s.version || "0.1.0",
    app: s.app || s.app_name || "",
    kind: s.kind || "generic",
    capability: s.capability || "",
    tags: (s.tags || s.intent_tags || []).join(", "),
    triggers: (s.triggers || []).join(", "),
    body: s.body || "",
    isNew: false,
  };
}

function parseList(s: string): string[] {
  return s
    .split(/[,，]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function groupCanonical(rows: SkillSummary[]) {
  const groups = new Map<string, SkillSummary[]>();
  for (const r of rows) {
    const key = r.app || r.app_name || "generic";
    const list = groups.get(key) || [];
    list.push(r);
    groups.set(key, list);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

export function SkillsView() {
  const [params, setParams] = useSearchParams();
  const deepId = params.get("id") || "";
  const tabParam = params.get("tab") === "pending" ? "pending" : "canonical";
  const [tab, setTab] = useState<TabId>(tabParam);
  const [selectedId, setSelectedId] = useState<string | null>(
    tabParam === "canonical" ? deepId || null : null,
  );
  const [pendingId, setPendingId] = useState<string | null>(
    tabParam === "pending" ? deepId || null : null,
  );
  const [editor, setEditor] = useState<EditorState>(emptyEditor());
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  useEffect(() => {
    setTab(tabParam);
  }, [tabParam]);

  useEffect(() => {
    if (!deepId) return;
    if (tabParam === "pending") setPendingId(deepId);
    else setSelectedId(deepId);
  }, [deepId, tabParam]);

  const canonicalQ = useQuery({
    queryKey: ["skills", "canonical"],
    queryFn: () => listSkills(),
  });
  const pendingQ = useQuery({
    queryKey: ["skills", "pending"],
    queryFn: listPendingSkills,
  });

  const detailQ = useQuery({
    queryKey: ["skill", selectedId],
    queryFn: () => getSkill(selectedId!),
    enabled: !!selectedId && tab === "canonical" && !editor.isNew,
  });

  const pendingDetailQ = useQuery({
    queryKey: ["skill-pending", pendingId],
    queryFn: () => getPendingSkill(pendingId!),
    enabled: !!pendingId && tab === "pending",
  });

  useEffect(() => {
    if (detailQ.data && tab === "canonical") {
      setEditor(fromSkill(detailQ.data));
      setError(null);
    }
  }, [detailQ.data, tab]);

  const groups = useMemo(
    () => groupCanonical(canonicalQ.data ?? []),
    [canonicalQ.data],
  );
  const pending = pendingQ.data ?? [];
  const pendingCount = pending.length;

  const saveMut = useMutation({
    mutationFn: async () => {
      const body = {
        id: editor.id.trim(),
        name: (editor.name || editor.id).trim(),
        description: editor.description.trim(),
        version: editor.version.trim() || "0.1.0",
        app: editor.app.trim() || null,
        kind: editor.kind,
        capability: editor.capability.trim(),
        tags: parseList(editor.tags),
        triggers: parseList(editor.triggers),
        body: editor.body,
      };
      if (editor.isNew) {
        return createSkill(body);
      }
      return updateSkill(editor.id, body);
    },
    onSuccess: (s) => {
      setError(null);
      setSelectedId(s.id);
      setEditor(fromSkill(s));
      setParams({ id: s.id, tab: "canonical" });
      qc.invalidateQueries({ queryKey: ["skills"] });
      qc.invalidateQueries({ queryKey: ["skill"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteSkill(editor.id),
    onSuccess: () => {
      setSelectedId(null);
      setEditor(emptyEditor());
      setParams({});
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const approveMut = useMutation({
    mutationFn: () => approvePendingSkill(pendingId!),
    onSuccess: () => {
      setPendingId(null);
      setParams({ tab: "pending" });
      qc.invalidateQueries({ queryKey: ["skills"] });
      qc.invalidateQueries({ queryKey: ["skill-pending"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const rejectMut = useMutation({
    mutationFn: () => rejectPendingSkill(pendingId!, "rejected from console"),
    onSuccess: () => {
      setPendingId(null);
      setParams({ tab: "pending" });
      qc.invalidateQueries({ queryKey: ["skills"] });
      qc.invalidateQueries({ queryKey: ["skill-pending"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  function selectCanonical(id: string) {
    setTab("canonical");
    setSelectedId(id);
    setEditor((e) => ({ ...e, isNew: false }));
    setParams({ id, tab: "canonical" });
  }

  function selectPending(row: PendingSkill) {
    setTab("pending");
    setPendingId(row.id);
    setError(null);
    setParams({ id: row.id, tab: "pending" });
  }

  function startNew() {
    setTab("canonical");
    setSelectedId(null);
    setPendingId(null);
    setEditor(emptyEditor());
    setParams({ tab: "canonical" });
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="font-mono text-sm font-semibold uppercase tracking-wide text-text">
          Skills
        </h1>
        <Button size="sm" variant="outline" onClick={startNew} className="font-mono text-[10px]">
          <Plus className="mr-1 h-3 w-3" /> 新建
        </Button>
      </div>

      <Tabs
        value={tab}
        onValueChange={(v) => {
          const next: TabId = v === "pending" ? "pending" : "canonical";
          setTab(next);
          setParams((p) => {
            const n = new URLSearchParams(p);
            n.set("tab", next);
            if (next === "canonical" && selectedId) n.set("id", selectedId);
            else if (next === "pending" && pendingId) n.set("id", pendingId);
            else n.delete("id");
            return n;
          });
        }}
      >
        <TabsList>
          <TabsTrigger value="canonical">正式技能</TabsTrigger>
          <TabsTrigger value="pending">
            待审 Pending
            {pendingCount > 0 && (
              <Badge variant="outline" className="ml-1 border-amber/40 text-amber">
                {pendingCount}
              </Badge>
            )}
          </TabsTrigger>
        </TabsList>

        <div className="mt-4 grid gap-4 lg:grid-cols-[280px_1fr]">
          <Card className="min-h-[28rem]">
            <CardHeader className="pb-2">
              <CardTitle className="font-mono text-[10px] uppercase text-text-mute">
                {tab === "canonical" ? "library" : "learner inbox"}
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-[70vh] space-y-2 overflow-auto p-3 pt-0">
              <TabsContent value="canonical" className="mt-0 space-y-3">
                {canonicalQ.isLoading && (
                  <div className="text-text-mute font-mono text-xs">loading…</div>
                )}
                {groups.map(([group, rows]) => (
                  <div key={group}>
                    <div className="mb-1 font-mono text-[10px] uppercase text-text-mute">
                      {group}
                    </div>
                    <ul className="space-y-0.5">
                      {rows.map((r) => (
                        <li key={r.id}>
                          <button
                            type="button"
                            onClick={() => selectCanonical(r.id)}
                            className={cn(
                              "w-full rounded border px-2 py-1 text-left font-mono text-[11px]",
                              selectedId === r.id && !editor.isNew
                                ? "border-neon/40 bg-neon/10 text-neon"
                                : "border-border text-text hover:border-border-hi",
                            )}
                          >
                            <span className="block truncate">{r.name || r.id}</span>
                            {r.description ? (
                              <span className="block truncate text-[10px] text-text-mute">
                                {r.description}
                              </span>
                            ) : null}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </TabsContent>
              <TabsContent value="pending" className="mt-0">
                {pendingQ.isLoading && (
                  <div className="text-text-mute font-mono text-xs">loading…</div>
                )}
                <ul className="space-y-0.5">
                  {pending.map((r) => (
                    <li key={r.id}>
                      <button
                        type="button"
                        onClick={() => selectPending(r)}
                        className={cn(
                          "w-full rounded border px-2 py-1 text-left font-mono text-[11px]",
                          pendingId === r.id
                            ? "border-amber/40 bg-amber/10 text-amber"
                            : "border-border text-text hover:border-border-hi",
                        )}
                      >
                        <span className="block truncate">{r.gist || r.id}</span>
                        <span className="block truncate text-[10px] text-text-mute">
                          {r.target}
                          {r.outcome ? ` · ${r.outcome}` : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                  {!pendingQ.isLoading && pending.length === 0 && (
                    <div className="font-mono text-xs text-text-mute">empty</div>
                  )}
                </ul>
              </TabsContent>
            </CardContent>
          </Card>

          {tab === "canonical" ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="font-mono text-[11px] uppercase tracking-wide text-cyan">
                  {editor.isNew ? "new skill" : editor.id || "editor"}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 font-mono text-xs">
                {error && (
                  <div className="rounded border border-err/40 bg-err/10 p-2 text-err">
                    {error}
                  </div>
                )}
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">id / name</span>
                  <input
                    className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.isNew ? editor.id : editor.name}
                    disabled={!editor.isNew}
                    onChange={(e) =>
                      setEditor({
                        ...editor,
                        id: e.target.value,
                        name: e.target.value,
                      })
                    }
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">description</span>
                  <input
                    className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.description}
                    onChange={(e) =>
                      setEditor({ ...editor, description: e.target.value })
                    }
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">version</span>
                  <input
                    className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.version}
                    onChange={(e) => setEditor({ ...editor, version: e.target.value })}
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">app</span>
                  <input
                    className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.app}
                    placeholder="empty = generic"
                    onChange={(e) => setEditor({ ...editor, app: e.target.value })}
                  />
                </label>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="block space-y-1">
                    <span className="text-[10px] uppercase text-text-mute">kind</span>
                    <select
                      className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                      value={editor.kind}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          kind: e.target.value as EditorState["kind"],
                        })
                      }
                    >
                      <option value="generic">generic</option>
                      <option value="app_core">app core</option>
                      <option value="workflow">workflow</option>
                      <option value="candidate">candidate</option>
                    </select>
                  </label>
                  <label className="block space-y-1">
                    <span className="text-[10px] uppercase text-text-mute">capability</span>
                    <input
                      className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                      value={editor.capability}
                      onChange={(e) =>
                        setEditor({ ...editor, capability: e.target.value })
                      }
                    />
                  </label>
                </div>
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">tags</span>
                  <input
                    className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.tags}
                    onChange={(e) => setEditor({ ...editor, tags: e.target.value })}
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">triggers</span>
                  <input
                    className="w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.triggers}
                    onChange={(e) => setEditor({ ...editor, triggers: e.target.value })}
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-[10px] uppercase text-text-mute">body (SKILL.md)</span>
                  <textarea
                    className="min-h-48 w-full rounded border border-border bg-bg-2 px-2 py-1 text-text"
                    value={editor.body}
                    onChange={(e) => setEditor({ ...editor, body: e.target.value })}
                  />
                </label>

                <div className="flex flex-wrap gap-2 pt-2">
                  <Button
                    size="sm"
                    disabled={!editor.id.trim() || saveMut.isPending}
                    onClick={() => saveMut.mutate()}
                    className="font-mono text-[10px]"
                  >
                    <Save className="mr-1 h-3 w-3" /> 保存
                  </Button>
                  {!editor.isNew && (
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={deleteMut.isPending}
                      onClick={() => {
                        if (!window.confirm(`确认删除正式技能 ${editor.id}？`)) return;
                        deleteMut.mutate();
                      }}
                      className="font-mono text-[10px]"
                    >
                      <Trash2 className="mr-1 h-3 w-3" /> 删除
                    </Button>
                  )}
                </div>
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="font-mono text-[11px] uppercase tracking-wide text-amber">
                  {pendingId || "pending review"}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 font-mono text-xs">
                {error && (
                  <div className="rounded border border-err/40 bg-err/10 p-2 text-err">
                    {error}
                  </div>
                )}
                {!pendingId && (
                  <div className="text-text-mute">选择左侧 pending 条目查看 diff</div>
                )}
                {pendingDetailQ.isLoading && (
                  <div className="text-text-mute">loading diff…</div>
                )}
                {pendingDetailQ.data && (
                  <>
                    <div className="space-y-1 text-text-mute">
                      <div>
                        gist:{" "}
                        <span className="text-text">{pendingDetailQ.data.gist}</span>
                      </div>
                      <div>
                        target:{" "}
                        <span className="text-cyan">{pendingDetailQ.data.target}</span>
                      </div>
                      {(() => {
                        const meta = pending.find((p) => p.id === pendingId);
                        if (!meta?.source_task_id) return null;
                        return (
                          <div>
                            source task{" "}
                            <Link
                              to={`/tasks/${meta.source_task_id}`}
                              className="text-cyan underline-offset-2 hover:underline"
                            >
                              {meta.source_task_id.slice(0, 12)}…
                            </Link>
                          </div>
                        );
                      })()}
                    </div>
                    <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap rounded border border-border bg-bg-2 p-2 text-[11px] text-text">
                      {pendingDetailQ.data.diff || "(empty diff)"}
                    </pre>
                    <div className="flex flex-wrap gap-2 pt-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={approveMut.isPending}
                        onClick={() => approveMut.mutate()}
                        className="font-mono text-[10px] border-neon/40 text-neon"
                      >
                        <Check className="mr-1 h-3 w-3" /> 批准
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={rejectMut.isPending}
                        onClick={() => rejectMut.mutate()}
                        className="font-mono text-[10px] border-err/40 text-err"
                      >
                        <X className="mr-1 h-3 w-3" /> 拒绝
                      </Button>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      </Tabs>
    </div>
  );
}
