import { useState } from "react";
import type { Action } from "@/api/types";
import { projectActionToFrame } from "@/lib/observationReplay";

/**
 * SoM image with an SVG overlay drawing the action's落点 at the exact
 * coordinates the driver acted on. The SVG viewBox matches the image's
 * natural size so coordinates map 1:1 and the overlay scales with the image.
 */
export function SomOverlay({
  somRef,
  action,
  frameGeometry,
  onImageSize,
}: {
  somRef: string | null | undefined;
  action: Action | null | undefined;
  /** Device coordinate space used by the dispatched action. */
  frameGeometry?: number[] | null;
  /**
   * live-screen-mirror (post-rail): bezel screen aspect adapts to the
   * SoM image's natural aspect ratio. Called once on `<img>` load with
   * `{ width, height }` so the host can size the frame to match the
   * captured screen — no letterbox black bars between the phone bezel
   * and the actual screenshot.
   */
  onImageSize?: (size: { width: number; height: number }) => void;
}) {
  const [errored, setErrored] = useState(false);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [hover, setHover] = useState<{ x: number; y: number } | null>(null);

  if (!somRef || errored) {
    return (
      <div className="flex h-48 items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
        无 SoM 记录
      </div>
    );
  }

  const src = `/api/artifacts/${somRef}`;
  const { width, height } = size;
  const displayAction = projectActionToFrame(action, frameGeometry, { width, height });
  const hasCoords = displayAction && (
    displayAction.x != null || displayAction.y != null
  );

  return (
    // live-screen-mirror (D28) + console-ui-quirk-fixes (D4): `h-full` on
    // the wrapper and `h-full w-full` on the <img> make the bezel's
    // aspect-matched inner screen (not the image's intrinsic size) the
    // binding constraint, so `object-contain` letterboxes any residual
    // source-vs-frame mismatch without overflow.
    <div className="relative flex h-full max-h-full max-w-full items-center justify-center">
      <img
        src={src}
        alt="SoM"
        className="block h-full w-full max-h-full max-w-full rounded-md border object-contain"
        onLoad={(e) => {
          const img = e.currentTarget;
          const w = img.naturalWidth;
          const h = img.naturalHeight;
          setSize({ width: w, height: h });
          onImageSize?.({ width: w, height: h });
        }}
        onError={() => setErrored(true)}
      />
      {hasCoords && width > 0 && height > 0 && (
        <svg
          className="pointer-events-none absolute inset-0 h-full w-full"
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="xMidYMid meet"
        >
          <OverlayShapes action={displayAction} onHover={setHover} />
        </svg>
      )}
      {hover && (
        <div className="pointer-events-none absolute left-2 top-2 rounded-md bg-black/80 px-1.5 py-0.5 text-[10px] text-white">
          ({Math.round(hover.x)}, {Math.round(hover.y)})
        </div>
      )}
    </div>
  );
}

function OverlayShapes({
  action,
  onHover,
}: {
  action: Action;
  onHover: (p: { x: number; y: number } | null) => void;
}) {
  const x = (action.x ?? 0) as number;
  const y = (action.y ?? 0) as number;
  const stroke = "#ef4444";
  const fill = "rgba(239,68,68,0.15)";

  if (action.type === "swipe" || action.type === "drag") {
    const x2 = (action.x2 ?? x) as number;
    const y2 = (action.y2 ?? y) as number;
    return (
      <g onMouseEnter={() => onHover({ x, y })} onMouseLeave={() => onHover(null)}>
        <line x1={x} y1={y} x2={x2} y2={y2} stroke={stroke} strokeWidth={6} strokeLinecap="round" />
        <circle cx={x} cy={y} r={8} fill={fill} stroke={stroke} strokeWidth={2} />
        <circle cx={x2} cy={y2} r={10} fill={fill} stroke={stroke} strokeWidth={2} />
        <ArrowHead x1={x} y1={y} x2={x2} y2={y2} stroke={stroke} />
      </g>
    );
  }

  if (action.type === "scroll") {
    const dir = action.direction ?? "down";
    const len = 80;
    const dx = dir === "left" ? -len : dir === "right" ? len : 0;
    const dy = dir === "up" ? -len : dir === "down" ? len : 0;
    const x2 = x + dx;
    const y2 = y + dy;
    return (
      <g onMouseEnter={() => onHover({ x, y })} onMouseLeave={() => onHover(null)}>
        <line x1={x} y1={y} x2={x2} y2={y2} stroke={stroke} strokeWidth={6} strokeLinecap="round" />
        <ArrowHead x1={x} y1={y} x2={x2} y2={y2} stroke={stroke} />
      </g>
    );
  }

  if (action.type === "type" || action.type === "replace_text" || action.type === "key") {
    if (action.x == null && action.y == null) return null;
    return (
      <circle
        cx={x}
        cy={y}
        r={14}
        fill="none"
        stroke="#3b82f6"
        strokeWidth={3}
        onMouseEnter={() => onHover({ x, y })}
        onMouseLeave={() => onHover(null)}
      />
    );
  }

  // tap / tap_xy / long_press → crosshair + circle
  const r = action.type === "long_press" ? 16 : 12;
  return (
    <g onMouseEnter={() => onHover({ x, y })} onMouseLeave={() => onHover(null)}>
      <circle cx={x} cy={y} r={r} fill={fill} stroke={stroke} strokeWidth={2} />
      <line x1={x - r - 4} y1={y} x2={x + r + 4} y2={y} stroke={stroke} strokeWidth={2} />
      <line x1={x} y1={y - r - 4} x2={x} y2={y + r + 4} stroke={stroke} strokeWidth={2} />
    </g>
  );
}

function ArrowHead({ x1, y1, x2, y2, stroke }: { x1: number; y1: number; x2: number; y2: number; stroke: string }) {
  const angle = Math.atan2(y2 - y1, x2 - x1);
  const len = 14;
  const a1 = angle - Math.PI / 6;
  const a2 = angle + Math.PI / 6;
  return (
    <>
      <line x1={x2} y1={y2} x2={x2 - len * Math.cos(a1)} y2={y2 - len * Math.sin(a1)} stroke={stroke} strokeWidth={6} strokeLinecap="round" />
      <line x1={x2} y1={y2} x2={x2 - len * Math.cos(a2)} y2={y2 - len * Math.sin(a2)} stroke={stroke} strokeWidth={6} strokeLinecap="round" />
    </>
  );
}
