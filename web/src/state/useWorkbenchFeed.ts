import { useEffect, useState } from "react";
import { openTaskStream } from "@/api/sse";
import type { TraceEvent } from "@/api/types";
import { mergeWorkbenchEvents, type FeedRow } from "@/lib/workbenchFeed";

export function useWorkbenchFeed(taskId: string, loaded: boolean, finished: boolean) {
  const [rows, setRows] = useState(() => new Map<string, FeedRow>());
  const [connection, setConnection] = useState<"connecting" | "open" | "retrying" | "closed">("connecting");
  useEffect(() => {
    if (!taskId || !loaded) return;
    const pending: TraceEvent[] = [];
    let animation = 0;
    const flush = () => {
      cancelAnimationFrame(animation);
      animation = 0;
      const batch = pending.splice(0);
      if (batch.length) setRows(previous => mergeWorkbenchEvents(previous, batch));
    };
    const stream = openTaskStream(taskId, {
      onEvent: event => {
        pending.push(event);
        if (!animation) animation = requestAnimationFrame(flush);
        if (pending.length >= 100) flush();
      },
      onOpen: () => setConnection("open"),
      onError: () => {
        flush();
        setConnection(finished ? "closed" : "retrying");
        return !finished;
      },
    }, "workbench");
    return () => { stream.close(); cancelAnimationFrame(animation); };
  }, [taskId, loaded, finished]);
  return { rows, connection };
}
