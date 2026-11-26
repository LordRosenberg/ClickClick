// EventSource wrapper for GET /api/tasks/{id}/stream with automatic reconnect.
// Each parsed SSE event is delivered to `onEvent`; the caller merges it into
// its React Query cache (see state/useTaskTimeline.ts).

import type { TraceEvent } from "./types";

export interface SseHandlers {
  onEvent: (event: TraceEvent) => void;
  onOpen?: () => void;
  onError?: (err: Event) => boolean | void;
}

const RECONNECT_DELAYS = [500, 1000, 2000, 5000];

export interface SseSubscription {
  close: () => void;
}

/**
 * Open an SSE stream for a task. Reconnects with backoff on error. Returns a
 * handle to close the stream (cancels any pending reconnect timer).
 */
export function openTaskStream(taskId: string, handlers: SseHandlers, view?: "workbench"): SseSubscription {
  let es: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let closed = false;

  const connect = () => {
    if (closed) return;
    const query = view ? `?view=${view}` : "";
    es = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/stream${query}`);

    es.onmessage = (msg) => {
      // The backend emits each event as a single `data:` JSON line.
      try {
        const parsed = JSON.parse(msg.data) as TraceEvent;
        handlers.onEvent(parsed);
      } catch {
        // Ignore malformed frames (e.g. keepalive comments never reach here).
      }
    };

    es.onerror = (err) => {
      const retry = handlers.onError?.(err);
      es?.close();
      es = null;
      if (closed || retry === false) return;
      const delay = RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)];
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    es.onopen = () => {
      attempt = 0;
      handlers.onOpen?.();
    };
  };

  connect();

  return {
    close: () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      es?.close();
      es = null;
    },
  };
}
