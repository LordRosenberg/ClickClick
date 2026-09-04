import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight, Radio } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SomOverlay } from "@/components/SomOverlay";
import { getScrcpyHint } from "@/api/client";
import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import {
  H264WebCodecsPlayer,
  isWebCodecsSupported,
  paintVideoFrame,
} from "@/lib/h264WebCodecs";
import type {
  Action,
  MirrorConnectionState,
  MirrorMode,
} from "@/api/types";

/** Reconnect policy (D19-D20). Exponential backoff 1 → 10s, max 5 attempts. */
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_CAP_MS = 10_000;
const RECONNECT_MAX_ATTEMPTS = 5;

const WS_CLOSE_TRY_AGAIN = 1013;

/**
 * Right-rail MirrorPanel — single canvas that switches between the live
 * scrcpy stream (Live mode) and the currently-selected tick's SoM image with
 * the action hit-point overlay (Frame mode). Lives on the task-detail page
 * at the `xl` breakpoint and below-the-inspector at `lg`; not rendered on
 * sub-lg viewports (D3, see TaskDetailView for the layout switch).
 *
 * The component owns its own connection lifecycle (no shared client): the
 * MirrorPanel is the only consumer of `/api/device/mirror/stream`, so the
 * backend's spawn-per-first / kill-on-last contract maps 1:1 to mount /
 * unmount of the panel. Mounting kicks off the WebSocket handshake;
 * unmounting drops the consumer and lets the registry tear down.
 */
export function MirrorPanel({
  taskId: _taskId,
  deviceSerial,
  deviceKey,
  selectedStepId,
  selectedSomRef,
  action,
  frameGeometry,
  className,
}: {
  taskId: string;
  /** ADB serial portion for Live hello. */
  deviceSerial?: string | null;
  /** Full binding key (e.g. lab-a/SERIAL); preferred for multi-hub routing. */
  deviceKey?: string | null;
  selectedStepId: number | null;
  selectedSomRef: string | null;
  action: Action | null;
  frameGeometry?: number[] | null;
  className?: string;
}) {
  const navigate = useNavigate();
  const [mode, setMode] = useState<MirrorMode>("live");
  const [connection, setConnection] = useState<MirrorConnectionState>("connecting");
  const [_hasFrame, setHasFrame] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const playerRef = useRef<H264WebCodecsPlayer | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedByUsRef = useRef(false);
  const connectGenRef = useRef(0);
  const deviceSerialRef = useRef(deviceSerial);
  const deviceKeyRef = useRef(deviceKey);
  deviceSerialRef.current = deviceSerial;
  deviceKeyRef.current = deviceKey;

  const scrcpyHint = useQuery({ queryKey: ["scrcpy"], queryFn: getScrcpyHint });

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const teardownPlayer = useCallback(() => {
    playerRef.current?.close();
    playerRef.current = null;
  }, []);

  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    setConnection("reconnecting");
    if (reconnectAttemptsRef.current >= RECONNECT_MAX_ATTEMPTS) {
      setConnection("disconnected");
      return;
    }
    const attempt = reconnectAttemptsRef.current + 1;
    reconnectAttemptsRef.current = attempt;
    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(2, attempt - 1),
      RECONNECT_CAP_MS
    );
    reconnectTimerRef.current = setTimeout(() => connectRef.current(), delay);
  }, []);

  const connect = useCallback(() => {
    clearReconnectTimer();
    closedByUsRef.current = false;
    teardownPlayer();
    const gen = ++connectGenRef.current;
    setConnection((prev) =>
      prev === "live" || prev === "unavailable" ? "connecting" : "reconnecting"
    );

    if (!isWebCodecsSupported()) {
      setConnection("unavailable");
      return;
    }

    try {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/api/device/mirror/stream`);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        if (gen !== connectGenRef.current) return;
        const serial = deviceSerialRef.current || "";
        const key = deviceKeyRef.current || serial;
        ws.send(JSON.stringify({ serial, device_key: key }));
        setConnection("connecting");
      };

      ws.onmessage = (ev) => {
        if (gen !== connectGenRef.current) return;
        if (typeof ev.data === "string") {
          try {
            const payload = JSON.parse(ev.data);
            if (payload && payload.type === "hello") {
              // Always paint via ref — Frame↔Live remounts must not trap an
              // old detached canvas in the decoder closure.
              if (!playerRef.current) {
                playerRef.current = new H264WebCodecsPlayer((frame) => {
                  const canvas = canvasRef.current;
                  if (canvas) {
                    paintVideoFrame(canvas, frame);
                    setHasFrame(true);
                  }
                }, payload.codec_string || "avc1.42E01E");
              } else if (payload.codec_string) {
                playerRef.current.setCodecString(payload.codec_string);
              }
              setConnection("live");
              reconnectAttemptsRef.current = 0;
            }
          } catch {
            // ignore non-JSON
          }
          return;
        }
        playerRef.current?.push(ev.data as ArrayBuffer);
      };

      ws.onclose = (ev) => {
        if (wsRef.current === ws) wsRef.current = null;
        if (gen !== connectGenRef.current) return;
        teardownPlayer();
        if (closedByUsRef.current) {
          setConnection("disconnected");
          return;
        }
        if (ev.code === WS_CLOSE_TRY_AGAIN) {
          setConnection("unavailable");
          return;
        }
        scheduleReconnect();
      };

      ws.onerror = () => {
        // onclose drives state
      };
    } catch {
      scheduleReconnect();
    }
  }, [clearReconnectTimer, teardownPlayer, scheduleReconnect]);

  connectRef.current = connect;

  useEffect(() => {
    if (!isWebCodecsSupported()) {
      setConnection("unavailable");
      return;
    }
    // Wait for the static probe so we don't open → teardown → reopen
    // when react-query flips `available` from undefined → true (that
    // churn surfaces as Vite "socket has been ended by the other party").
    if (!scrcpyHint.data && scrcpyHint.isLoading) {
      return;
    }
    if (scrcpyHint.data?.available === false) {
      setConnection("unavailable");
      return;
    }
    if (!deviceSerial && !deviceKey) {
      setConnection("disconnected");
      return;
    }
    connect();
    return () => {
      closedByUsRef.current = true;
      connectGenRef.current += 1;
      clearReconnectTimer();
      teardownPlayer();
      const ws = wsRef.current;
      if (ws) {
        try {
          ws.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
    // Intentionally depend on serial/key/availability, not `connect` identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    clearReconnectTimer,
    teardownPlayer,
    scrcpyHint.isLoading,
    scrcpyHint.data?.available,
    deviceSerial,
    deviceKey,
  ]);

  // Effect: when the user selects a tick on the timeline, auto-flip to
  // Frame mode (D12). We deliberately DON'T clear `selectedStepId` from
  // the parent — the inspector keeps its selection.
  useEffect(() => {
    if (selectedStepId != null && selectedSomRef) {
      setMode("frame");
    }
  }, [selectedStepId, selectedSomRef]);

  const goLive = useCallback(() => {
    setMode("live");
  }, []);

  return (
    <Card className={cn("border-border bg-bg-1", className)}>
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide text-text-mute">
            <Radio className="h-3.5 w-3.5" /> 设备投屏
          </CardTitle>
          <ConnectionDot state={connection} />
          <Badge variant="outline" className="border-border bg-bg-2 font-mono text-[10px] text-text-mute">
            {mode === "live" ? "live" : "frame"}
          </Badge>
          <div className="ml-auto">
            {mode === "frame" ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={goLive}
                className="font-mono text-[10px] uppercase tracking-wide"
              >
                Live
              </Button>
            ) : null}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {/* live-screen-mirror (D30): collapsible usage guide sits directly
            under the header so first-time operators know how to flip modes
            and what each state means. Default folded. */}
        <details className="group mb-3 rounded border border-border bg-bg-1">
          <summary className="flex cursor-pointer items-center gap-2 px-2 py-1 font-mono text-[10px] uppercase tracking-wide text-text-mute hover:bg-bg-2">
            <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
            使用说明
          </summary>
          <ol className="space-y-1 border-t border-border p-2 font-mono text-[10px] leading-relaxed text-text-mute">
            <li>
              <span className="text-text">1.</span> 点击时间轴上的步骤 →
              切到 Frame 模式，显示该步的 SoM 标注图（含落点红圈 / 箭头）。
            </li>
            <li>
              <span className="text-text">2.</span> 点头部的{" "}
              <span className="rounded border border-border bg-bg-2 px-1 text-text">
                Live
              </span>{" "}
              按钮 → 回到实时投屏（首次需等 scrcpy 就绪，约 1~2s）。
            </li>
            <li>
              <span className="text-text">3.</span> 屏幕黑或卡住 → 点{" "}
              <span className="rounded border border-border bg-bg-2 px-1 text-text">
                立即重试
              </span>
              ；服务端缺 scrcpy-server / 无 WebCodecs 时会显示「投屏不可用」，但 Console 其他功能不受影响。
            </li>
          </ol>
        </details>

        <MirrorCanvas
          mode={mode}
          connection={connection}
          canvasRef={canvasRef}
          selectedSomRef={selectedSomRef}
          action={action}
          frameGeometry={frameGeometry}
          onManualRetry={connect}
          onNavigateDevice={() => navigate("/device")}
        />
      </CardContent>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/*  Connection dot                                                            */
/* -------------------------------------------------------------------------- */

function ConnectionDot({ state }: { state: MirrorConnectionState }) {
  const color =
    state === "live"
      ? "bg-neon"
      : state === "reconnecting"
        ? "bg-amber"
        : state === "unavailable"
          ? "bg-text-mute"
          : "bg-err";
  const label =
    state === "live"
      ? "live"
      : state === "reconnecting"
        ? "reconnecting"
        : state === "connecting"
          ? "connecting"
          : state === "unavailable"
            ? "unavailable"
            : "disconnected";
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-wide text-text-mute">
      <span className={cn("inline-block h-1.5 w-1.5 rounded-full", color)} />
      {label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  Canvas (chrome + reconnect / unavailable overlays)                         */
/* -------------------------------------------------------------------------- */

function MirrorCanvas({
  mode,
  connection,
  canvasRef,
  selectedSomRef,
  action,
  frameGeometry,
  onManualRetry,
  onNavigateDevice,
}: {
  mode: MirrorMode;
  connection: MirrorConnectionState;
  canvasRef: React.RefObject<HTMLCanvasElement>;
  selectedSomRef: string | null;
  action: Action | null;
  frameGeometry?: number[] | null;
  onManualRetry: () => void;
  onNavigateDevice: () => void;
}) {
  // live-screen-mirror (D29) + timeline-round-rail (bezel-fit):
  // CSS-only phone-shell bezel. The outer rounded wrapper holds the
  // bezel + earpiece + home indicator; the inner screen mounts the
  // existing 9:16 canvas / <img> with `inset-3` padding so the screen
  // sits visually recessed inside the bezel.
  //
  // The bezel fills the column (`w-full`) instead of being capped at
  // 360px, so it adapts to whatever width the column gives it. The
  // screen's aspect ratio is driven by the SoM image's natural size
  // (captured by SomOverlay.onImageSize); defaults to 9:16 when no
  // image has loaded yet. This eliminates the black bars the operator
  // saw between a 9:16 bezel and a non-9:16 captured screen.
  const [screenAspect, setScreenAspect] = useState<number>(9 / 16);

  // Reset the aspect when the selected step changes — the next
  // <img> load will set it again.
  useEffect(() => {
    setScreenAspect(9 / 16);
  }, [selectedSomRef]);

  const bezel =
    "relative mx-auto w-full max-w-[420px] rounded-[28px] border-[2px] border-border-hi bg-bg-2 p-3";
  const earpiece =
    "absolute top-2 left-1/2 -translate-x-1/2 h-1 w-12 rounded-full bg-bg-3";
  const homeIndicator =
    "absolute bottom-2 left-1/2 -translate-x-1/2 h-1 w-10 rounded-full bg-bg-3";
  // Inner screen — width is the column width (minus bezel padding +
  // border); height is `width / aspectRatio`. The aspect ratio is a
  // CSS variable so SomOverlay's onLoad callback can update it without
  // re-rendering the whole bezel tree.
  const screenBase =
    "relative w-full overflow-hidden rounded-[20px] bg-black";

  const screenStyle: React.CSSProperties = {
    aspectRatio: `${screenAspect}`,
  };

  const handleImageSize = useCallback((size: { width: number; height: number }) => {
    if (size.width > 0 && size.height > 0) {
      setScreenAspect(size.width / size.height);
    }
  }, []);

  // Keep the live <canvas> mounted in both modes so hello/binary can
  // create the decoder while the operator is briefly on Frame, and so
  // Live↔Frame flips don't detach the paint target.
  return (
    <div className={bezel}>
      <span className={earpiece} aria-hidden />
      <div className={screenBase} style={screenStyle}>
        <canvas
          ref={canvasRef}
          width={360}
          height={640}
          className={cn(
            "absolute inset-0 h-full w-full object-contain",
            mode !== "live" && "invisible"
          )}
        />
        {mode === "frame" && (
          <div className="absolute inset-0 flex items-center justify-center">
            <SomOverlay
              somRef={selectedSomRef}
              action={action}
              frameGeometry={frameGeometry}
              onImageSize={handleImageSize}
            />
          </div>
        )}
        {mode === "live" && connection === "connecting" && (
          <OverlayCenter>连接中…</OverlayCenter>
        )}
        {mode === "live" && connection === "reconnecting" && (
          <OverlayCenter>
            <div className="space-y-2 text-center">
              <div className="font-mono text-[11px] uppercase tracking-wide text-text">
                Reconnecting…
              </div>
              <div className="font-mono text-[10px] text-text-mute">
                自动重连中；如多次失败请使用下方手动重试按钮。
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={onManualRetry}
                className="font-mono text-[10px] uppercase"
              >
                立即重试
              </Button>
            </div>
          </OverlayCenter>
        )}
        {mode === "live" && connection === "disconnected" && (
          <OverlayCenter>
            <div className="space-y-2 text-center">
              <div className="font-mono text-[11px] uppercase tracking-wide text-err">
                Disconnected
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={onManualRetry}
                className="font-mono text-[10px] uppercase"
              >
                立即重试
              </Button>
            </div>
          </OverlayCenter>
        )}
        {mode === "live" && connection === "unavailable" && (
          <OverlayCenter>
            <div className="space-y-2 text-center text-text-mute">
              <div className="font-mono text-[11px] uppercase tracking-wide text-text">
                投屏不可用
              </div>
              <div className="px-4 font-mono text-[10px] leading-relaxed">
                需要 Chromium WebCodecs，且 API/远端 hub 上有 vendored{" "}
                <span className="text-cyan">scrcpy-server</span>。Console
                的任务管理与调试不依赖投屏；也可在有 adb 的主机上单独运行桌面{" "}
                <span className="rounded border border-border bg-bg-2 px-1 text-cyan">
                  scrcpy
                </span>
                。
              </div>
              <button
                type="button"
                onClick={onNavigateDevice}
                className="font-mono text-[10px] uppercase tracking-wide text-cyan underline-offset-2 hover:underline"
              >
                打开 Device 页 →
              </button>
            </div>
          </OverlayCenter>
        )}
      </div>
      <span className={homeIndicator} aria-hidden />
    </div>
  );
}

function OverlayCenter({ children }: { children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      {children}
    </div>
  );
}
