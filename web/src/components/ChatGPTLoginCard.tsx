import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  getChatGPTStatus,
  pollChatGPTLogin,
  startChatGPTLogin,
} from "@/api/client";
import type { ChatGPTLoginStart } from "@/api/types";

export function ChatGPTLoginCard() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["chatgpt", "status"],
    queryFn: getChatGPTStatus,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    // Status is local-file only; no need to hammer the API every 15s.
    refetchInterval: false,
  });
  const [pending, setPending] = useState<ChatGPTLoginStart | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);

  const stopPolling = () => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  };

  useEffect(() => () => stopPolling(), []);

  const start = useMutation({
    mutationFn: startChatGPTLogin,
    onSuccess: (data) => {
      setPollError(null);
      setPending(data);
      stopPolling();
      const intervalMs = Math.max(3, data.interval_s || 5) * 1000;
      pollTimer.current = window.setInterval(async () => {
        try {
          const result = await pollChatGPTLogin();
          if (result.status === "pending") {
            setPending({
              user_code: result.user_code || data.user_code,
              verify_url: result.verify_url || data.verify_url,
              interval_s: result.interval_s || data.interval_s,
            });
            return;
          }
          stopPolling();
          setPending(null);
          if (result.status === "error") {
            setPollError(result.error || "login failed");
            return;
          }
          qc.invalidateQueries({ queryKey: ["chatgpt", "status"] });
        } catch (err) {
          stopPolling();
          setPollError((err as Error)?.message || "poll failed");
        }
      }, intervalMs);
    },
    onError: (err) => {
      setPollError((err as Error)?.message || "start failed");
    },
  });

  const authenticated = !!status.data?.authenticated;

  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 font-mono text-xs uppercase tracking-wider text-text-mute">
          ▶ chatgpt pro
          <Badge variant={authenticated ? "success" : "secondary"}>
            {status.isLoading ? "…" : authenticated ? "signed in" : "signed out"}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 font-mono text-[11px] text-text-mute">
        {authenticated && status.data?.account_id && (
          <div className="truncate text-text">account: {status.data.account_id}</div>
        )}
        {!authenticated && !pending && (
          <p>
            Sign in with a ChatGPT Pro subscription to use <span className="text-text">chatgpt/*</span>{" "}
            models alongside relay API keys.
          </p>
        )}
        {pending && (
          <div className="space-y-1 rounded border border-border bg-bg-2 p-2 text-text">
            <div>
              code: <span className="text-neon">{pending.user_code}</span>
            </div>
            <a
              href={pending.verify_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-cyan underline-offset-2 hover:underline"
            >
              open verify URL <ExternalLink className="h-3 w-3" />
            </a>
            <div className="text-[10px] text-text-mute">waiting for browser authorization…</div>
          </div>
        )}
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={start.isPending || !!pending}
            className="font-mono text-[11px] uppercase tracking-wider"
            onClick={() => start.mutate()}
          >
            {pending ? "waiting…" : authenticated ? "re-login" : "login"}
          </Button>
          {(pollError || start.isError) && (
            <span className="text-err">
              {pollError || (start.error as Error)?.message?.slice(0, 80)}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
