import { useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const INDEX_LINE = /^\s*\[(\d+)\]/;

/**
 * Render the `text_for_llm` semantic tree as preformatted text. Lines that
 * declare an interactable element (`[<index>]`) are highlighted; the action's
 * target index line gets a background highlight. A tier badge is shown when
 * the tree metadata is available, and the view can collapse to only the
 * interactable-element lines. The fold / unfold affordance lives in the host
 * (StepInspector), which owns the surrounding `<details>` element; this
 * component just renders the body (D26).
 */
export function SemanticTree({
  textForLlm,
  targetIndex,
  tier,
}: {
  textForLlm: string | null | undefined;
  targetIndex: number | null | undefined;
  tier?: string | null;
}) {
  const [interactableOnly, setInteractableOnly] = useState(false);

  const lines = useMemo(() => (textForLlm ?? "").split("\n"), [textForLlm]);

  const visibleLines = useMemo(
    () => (interactableOnly ? lines.filter((l) => INDEX_LINE.test(l)) : lines),
    [lines, interactableOnly]
  );

  const scrollRef = useRef<HTMLDivElement>(null);
  const targetRef = useRef<HTMLSpanElement>(null);

  // When the target line / text changes, vertically center the highlighted
  // target line in the scrollable body. Effect runs after layout; the inner
  // requestAnimationFrame is a safety net so the host <details> panel has
  // measured its height before we measure. We use getBoundingClientRect (not
  // offsetTop) because offsetTop is relative to the offsetParent, which can
  // be any positioned ancestor — not the scrollable container.
  useEffect(() => {
    const raf = requestAnimationFrame(() => {
      const scrollEl = scrollRef.current;
      const targetEl = targetRef.current;
      if (!scrollEl || !targetEl) return;
      const spanRect = targetEl.getBoundingClientRect();
      const preRect = scrollEl.getBoundingClientRect();
      const spanTopWithinPre = spanRect.top - preRect.top + scrollEl.scrollTop;
      const desired =
        spanTopWithinPre - scrollEl.clientHeight / 2 + targetEl.clientHeight / 2;
      const max = scrollEl.scrollHeight - scrollEl.clientHeight;
      scrollEl.scrollTop = Math.max(0, Math.min(desired, max));
    });
    return () => cancelAnimationFrame(raf);
  }, [textForLlm, targetIndex]);

  if (!textForLlm) {
    return (
      <div className="font-mono text-[10px] text-text-mute">
        — no semantic tree recorded
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* live-screen-mirror (D8b): tier + target-index badges sit on a
          single summary-style row above the tree, mirroring the host
          StepInspector's fold styling. The host owns the actual <details>
          fold; this row is purely informational. */}
      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[10px] uppercase tracking-wide text-text-mute">
        {tier && (
          <Badge
            variant="outline"
            className="border-cyan/40 bg-cyan/10 font-mono text-[9px] text-cyan"
          >
            tier: {tier}
          </Badge>
        )}
        {targetIndex != null && (
          <Badge
            variant="outline"
            className="border-amber/40 bg-amber/10 font-mono text-[9px] text-amber"
          >
            idx {targetIndex}
          </Badge>
        )}
        <span className="font-mono text-[9px] text-text-mute/60">
          {lines.length} lines
        </span>
      </div>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant={interactableOnly ? "secondary" : "ghost"}
          size="sm"
          onClick={() => setInteractableOnly((v) => !v)}
          className="font-mono text-[10px] uppercase"
        >
          {interactableOnly ? "仅可交互" : "全部"}
        </Button>
      </div>
      {/* live-screen-mirror (D8b): horizontally scrollable so long
          semantic-tree lines (which have no whitespace break points) can
          be panned left/right inside the inspector column rather than
          pushing the column wider. console-ui-quirk-fixes (D1): `min-w-0`
          completes the chain from the middle Card down to here. */}
      <div
        ref={scrollRef}
        className="max-h-[420px] min-w-0 overflow-auto whitespace-pre rounded border border-border bg-bg-2 p-2 font-mono text-[10px] leading-relaxed"
      >
        {visibleLines.map((line, i) => {
          const m = INDEX_LINE.exec(line);
          const idx = m ? Number(m[1]) : null;
          const isTarget = idx != null && idx === targetIndex;
          const isInteractable = idx != null;
          return (
            <span
              key={i}
              ref={isTarget ? targetRef : undefined}
              className={cn(
                "block w-full",
                isTarget &&
                  "rounded bg-amber/20 text-amber ring-1 ring-amber/40",
                !isTarget && isInteractable && "text-text",
                !isInteractable && "text-text-mute/60",
              )}
            >
              {line || " "}
            </span>
          );
        })}
      </div>
    </div>
  );
}