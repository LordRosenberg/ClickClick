import { useEffect, useMemo, useRef, useState } from "react";

import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { getArtifactText } from "@/api/client";

/**
 * Color tokens (LlmViewer-internal — not Tailwind theme tokens):
 *   key      — cyan      (cyan-300 / #67e8f9)
 *   string   — neon-soft (mint / #6ee7b7)
 *   number   — amber     (#fbbf24)
 *   boolean  — violet    (#a78bfa)
 *   null     — muted red (#f87171)
 *   punct    — text-mute (#7a869a)
 */
type Token = { kind: "key" | "string" | "number" | "boolean" | "null" | "punct" | "ws"; text: string };

/**
 * Tokenize a JSON document into colored spans. Indentation and newlines
 * become their own ws tokens so the viewer preserves original whitespace.
 */
function tokenizeJson(input: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  const len = input.length;
  while (i < len) {
    const ch = input[i];
    // whitespace
    if (ch === " " || ch === "\t" || ch === "\n" || ch === "\r") {
      let j = i;
      while (j < len && /[ \t\n\r]/.test(input[j])) j++;
      tokens.push({ kind: "ws", text: input.slice(i, j) });
      i = j;
      continue;
    }
    // string
    if (ch === '"') {
      let j = i + 1;
      while (j < len) {
        if (input[j] === "\\") {
          j += 2;
          continue;
        }
        if (input[j] === '"') {
          j++;
          break;
        }
        j++;
      }
      // peek for trailing colon → key
      let k = j;
      while (k < len && /[ \t\n\r]/.test(input[k])) k++;
      const isKey = input[k] === ":";
      tokens.push({ kind: isKey ? "key" : "string", text: input.slice(i, j) });
      i = j;
      continue;
    }
    // number
    if (ch === "-" || (ch >= "0" && ch <= "9")) {
      let j = i + 1;
      while (j < len && /[0-9.eE+\-]/.test(input[j])) j++;
      tokens.push({ kind: "number", text: input.slice(i, j) });
      i = j;
      continue;
    }
    // literals (true / false / null)
    if (input.startsWith("true", i)) {
      tokens.push({ kind: "boolean", text: "true" });
      i += 4;
      continue;
    }
    if (input.startsWith("false", i)) {
      tokens.push({ kind: "boolean", text: "false" });
      i += 5;
      continue;
    }
    if (input.startsWith("null", i)) {
      tokens.push({ kind: "null", text: "null" });
      i += 4;
      continue;
    }
    // punctuation
    if ("{}[],:".includes(ch)) {
      tokens.push({ kind: "punct", text: ch });
      i++;
      continue;
    }
    // fallback — pass through
    tokens.push({ kind: "ws", text: ch });
    i++;
  }
  return tokens;
}

const TOKEN_CLASS: Record<Token["kind"], string> = {
  key: "text-cyan-300",
  string: "text-emerald-300/90",
  number: "text-amber",
  boolean: "text-violet",
  null: "text-rose-400",
  punct: "text-text-mute",
  ws: "",
};

/**
 * Try to detect & pretty-format JSON; otherwise return as plain text.
 */
function prettyOrRaw(input: string): string {
  const trimmed = input.trim();
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      // not valid JSON — fall through
    }
  }
  return input;
}

/**
 * Expandable viewer for an LLM input/output artifact ref. Fetches the raw
 * artifact text on first expand and caches it. Renders JSON with cyan keys /
 * mint strings / amber numbers / violet booleans / rose nulls.
 */
export function LlmViewer({
  label,
  refId,
}: {
  label: string;
  refId: string | null | undefined;
}) {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Track the last refId we have a cached fetch for. When the prop changes
  // (e.g., operator selects a different step in the timeline), reset the
  // fetch state so the next render kicks off a fresh fetch for the new
  // refId. This runs during render and is idempotent — safe under StrictMode.
  const lastRefIdRef = useRef<string | null | undefined>(refId);
  if (lastRefIdRef.current !== refId) {
    lastRefIdRef.current = refId;
    setContent(null);
    setLoading(false);
    setError(null);
  }

  // Auto-collapse when the role that produced the artifact disappears
  // (refId transitions to null on a later step). Keeps the UI honest: a
  // collapsed viewer with no content should not stay open.
  const prevRefIdRef = useRef<string | null | undefined>(refId);
  useEffect(() => {
    const prev = prevRefIdRef.current;
    prevRefIdRef.current = refId;
    if (prev != null && refId == null) {
      setOpen(false);
    }
  }, [refId]);

  // Fetch the artifact body for the current refId. Re-runs whenever refId
  // changes (parent switched steps), or when the user first expands the
  // panel. Content is cached in component state for the lifetime of the
  // mount, so collapsing-then-reopening the same step does NOT re-fetch.
  useEffect(() => {
    if (!refId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getArtifactText(refId)
      .then((text) => {
        if (cancelled) return;
        setContent(text);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refId]);

  const prettyContent = useMemo(
    () => (content == null ? null : prettyOrRaw(content)),
    [content],
  );
  const tokens = useMemo(
    () => (prettyContent == null ? [] : tokenizeJson(prettyContent)),
    [prettyContent],
  );

  if (!refId) {
    return (
      <div className="font-mono text-[10px] uppercase tracking-wide text-text-mute">
        {label}: — no record
      </div>
    );
  }

  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    // First expand on this refId: kick off the fetch. The mount/refresh
    // effect above already populated `content` for refIds known at mount,
    // but if the user expands before that effect resolved, this guarantees
    // a fetch is in flight.
    setOpen(true);
    if (content === null && !loading && refId) {
      setLoading(true);
      setError(null);
      try {
        const text = await getArtifactText(refId);
        setContent(text);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    }
  };

  return (
    <div className="rounded border border-border bg-bg-1">
      <button
        type="button"
        onClick={toggle}
        // console-ui-quirk-fixes (D3): the refId sits to the LEFT of the
        // label so it reads in full inside the inspector column instead of
        // being right-clipped at the column edge. `truncate` (no max-w cap)
        // stays as a safety net for unusually long artifact paths, and
        // `min-w-0` on this flex parent is what makes it engage.
        title={refId ?? undefined}
        className="flex w-full min-w-0 items-center gap-2 px-2 py-1 hover:bg-bg-2"
      >
        <ChevronRight
          className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")}
        />
        <span className="truncate font-mono text-[10px] text-text-mute/60">
          {refId}
        </span>
        <span className="ml-auto shrink-0 font-mono text-[11px] uppercase tracking-wide text-text">
          {label}
        </span>
      </button>
      {open && (
        <pre className="max-h-72 overflow-auto border-t border-border bg-bg-2 p-2 font-mono text-xs whitespace-pre-wrap">
          {loading ? (
            <span className="text-text-mute">loading…</span>
          ) : error ? (
            <span className="text-err">error: {error}</span>
          ) : (
            tokens.map((t, i) => (
              <span key={i} className={TOKEN_CLASS[t.kind]}>
                {t.text}
              </span>
            ))
          )}
        </pre>
      )}
    </div>
  );
}