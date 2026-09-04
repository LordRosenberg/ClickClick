import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind classes with conditional support (shadcn/ui convention). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Format a millisecond duration for Console chips.
 *
 * - `null` / `undefined` → `"— ms"` (graceful absence, used for
 *   current records where timing is unavailable).
 * - `≤ 9999 ms` → `"1234 ms"` so short steps stay grep-friendly.
 * - `≥ 10000 ms` → `"12.3s"` rounded to one decimal, so a 60-second
 *   task reads `60.0s` rather than `60000 ms`.
 *
 * Keeps chip widths stable across runs.
 */
export function formatMs(ms?: number | null): string {
  if (ms == null || Number.isNaN(ms)) return "— ms";
  if (ms < 10000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Format an epoch-seconds timestamp in the Console's local timezone. */
export function formatWallTime(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const value = new Date(seconds * 1000);
  if (Number.isNaN(value.getTime())) return "—";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${value.getFullYear()}/${pad(value.getMonth() + 1)}/${pad(value.getDate())} ${pad(value.getHours())}:${pad(value.getMinutes())}`;
}
