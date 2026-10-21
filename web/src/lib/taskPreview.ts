// The browser consumes published PNG snapshots; it never sends phone actions.
export interface PreviewFrame {
  task_id: string;
  device_key: string;
  run_epoch: string;
  provider_generation: number;
  frame_id: number;
  width: number;
  height: number;
  geometry: Record<string, number | null>;
  frame_timestamp: number;
  frame_age_ms: number;
  observed_at: number;
}

export interface PreviewState {
  task_id: string;
  task_status?: string;
  device_key?: string;
  preview_state: "available" | "waiting" | "unavailable";
  reason: string;
  frame: PreviewFrame | null;
}

export interface PreviewView {
  state: PreviewState | null;
  frame: PreviewFrame | null;
  imageUrl: string | null;
  live: boolean;
  error: string | null;
  ageMs: number;
}

export interface PreviewEnvironment {
  fetch: typeof fetch;
  later: (fn: () => void, ms: number) => unknown;
  cancel: (timer: unknown) => void;
  hidden: () => boolean;
  visibility: (fn: () => void) => () => void;
  createUrl: (blob: Blob) => string;
  revokeUrl: (url: string) => void;
  now: () => number;
}

export function sameRun(a: PreviewFrame, b: PreviewFrame): boolean {
  return a.task_id === b.task_id && a.device_key === b.device_key &&
    a.run_epoch === b.run_epoch && a.provider_generation === b.provider_generation;
}

export function parsePreviewFrame(value: unknown, taskId: string): PreviewFrame {
  const f = value as PreviewFrame;
  if (!f || f.task_id !== taskId || typeof f.device_key !== "string" ||
    typeof f.run_epoch !== "string" || !f.run_epoch || f.run_epoch.length > 128 ||
    ![f.provider_generation, f.frame_id].every(n => Number.isSafeInteger(n) && n >= 0) ||
    ![f.width, f.height].every(n => Number.isSafeInteger(n) && n > 0) ||
    ![f.frame_timestamp, f.frame_age_ms, f.observed_at].every(n => Number.isFinite(n) && n >= 0) ||
    !f.geometry || f.geometry.stream_width !== f.width || f.geometry.stream_height !== f.height) {
    throw new Error("invalid_preview_frame");
  }
  return f;
}

function browserEnvironment(): PreviewEnvironment {
  return {
    fetch: (input, init) => fetch(input, init),
    later: (fn, ms) => window.setTimeout(fn, ms),
    cancel: timer => window.clearTimeout(timer as number),
    hidden: () => document.hidden,
    visibility: fn => {
      document.addEventListener("visibilitychange", fn);
      return () => document.removeEventListener("visibilitychange", fn);
    },
    createUrl: blob => URL.createObjectURL(blob),
    revokeUrl: url => URL.revokeObjectURL(url), now: () => performance.now(),
  };
}

export function startTaskPreview(
  taskId: string, update: (view: PreviewView) => void,
  env: PreviewEnvironment = browserEnvironment(),
): () => void {
  const root = `/api/tasks/${encodeURIComponent(taskId)}/preview`;
  let closed = false;
  let sequence = 0;
  let timer: unknown;
  let abort: AbortController | null = null;
  let attempt = 0;
  let receivedAt = 0;
  let view: PreviewView = { state: null, frame: null, imageUrl: null,
    live: false, error: null, ageMs: 0 };
  const emit = () => update({ ...view, ageMs: view.frame
    ? view.frame.frame_age_ms + Math.max(0, env.now() - receivedAt) : 0 });
  const dropImage = () => {
    if (view.imageUrl) env.revokeUrl(view.imageUrl);
    view = { ...view, imageUrl: null, frame: null, live: false };
  };
  const backoff = () => [1000, 2000, 5000][Math.min(attempt++, 2)];
  const retryDelay = (response: Response) => {
    const seconds = Number(response.headers.get("Retry-After") || 1);
    return Number.isFinite(seconds) ? Math.max(1000, seconds * 1000) : 1000;
  };

  async function tick() {
    if (closed || env.hidden()) return;
    const seq = ++sequence;
    const active = () => !closed && !env.hidden() && seq === sequence;
    abort = new AbortController();
    const options = { signal: abort.signal, cache: "no-store" as const, credentials: "same-origin" as const };
    let delay = 500;
    try {
      const response = await env.fetch(`${root}/state`, options);
      if (!active()) return;
      if (response.status === 429) {
        delay = retryDelay(response);
        throw new Error("preview_busy");
      }
      if (!response.ok) throw new Error(response.status === 404 ? "task_not_found" : "preview_unavailable");
      const state = await response.json() as PreviewState;
      if (!active()) return;
      if (state.task_id !== taskId || !["available", "waiting", "unavailable"].includes(state.preview_state)) {
        throw new Error("invalid_preview_state");
      }
      view = { ...view, state, live: false, error: null };
      if (state.preview_state !== "available" || !state.frame) {
        delay = backoff();
        emit();
        return;
      }
      const advertised = parsePreviewFrame(state.frame, taskId);
      if (view.frame && !sameRun(view.frame, advertised)) dropImage();
      if (view.frame && advertised.frame_id <= view.frame.frame_id) {
        // A state response can precede an already displayed, newer PNG snapshot.
        if (advertised.frame_id === view.frame.frame_id) {
          view = { ...view, frame: advertised };
          receivedAt = env.now();
        }
        view = { ...view, live: true };
        attempt = 0;
        emit();
        return;
      }
      const query = new URLSearchParams({ run_epoch: advertised.run_epoch,
        generation: String(advertised.provider_generation),
        after_frame_id: String(view.frame?.frame_id ?? -1) });
      const pixels = await env.fetch(`${root}/frame?${query}`, options);
      if (!active()) return;
      if (pixels.status === 409) { emit(); return; }
      if (pixels.status === 429) {
        delay = retryDelay(pixels);
        throw new Error("preview_busy");
      }
      if (!pixels.ok) throw new Error("preview_unavailable");
      if (pixels.status === 204) {
        view = { ...view, live: Boolean(view.frame) };
        attempt = 0;
        emit();
        return;
      }
      const header = pixels.headers.get("X-ClickClick-Preview-Frame");
      if (!header || header.length > 4096 || !pixels.headers.get("Content-Type")?.startsWith("image/png")) {
        throw new Error("invalid_preview_frame");
      }
      const actual = parsePreviewFrame(JSON.parse(header), taskId);
      if (!sameRun(actual, advertised) || (view.frame && actual.frame_id <= view.frame.frame_id)) {
        throw new Error("invalid_preview_frame");
      }
      const blob = await pixels.blob();
      if (!active()) return;
      const nextUrl = env.createUrl(blob);
      if (view.imageUrl) env.revokeUrl(view.imageUrl);
      receivedAt = env.now();
      view = { ...view, frame: actual, imageUrl: nextUrl, live: true, error: null };
      attempt = 0;
      emit();
    } catch (error) {
      if (!active()) return;
      const reason = error instanceof Error ? error.message : "preview_unavailable";
      if (reason !== "preview_busy") delay = backoff();
      view = { ...view, live: false, error: reason };
      emit();
    } finally {
      if (active()) timer = env.later(() => { void tick(); }, delay);
    }
  }

  const unsubscribe = env.visibility(() => {
    sequence++;
    abort?.abort();
    env.cancel(timer);
    view = { ...view, live: false };
    emit();
    if (!env.hidden()) void tick();
  });
  emit();
  void tick();
  return () => {
    closed = true;
    sequence++;
    abort?.abort();
    env.cancel(timer);
    unsubscribe();
    dropImage();
  };
}
