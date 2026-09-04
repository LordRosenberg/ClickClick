import type { Action, RoleCallRole, StepObservation } from "@/api/types";

export interface ImageGeometry {
  width: number;
  height: number;
}

export function observationFromTracePayload(
  payload: Record<string, unknown>,
): StepObservation {
  const observation: StepObservation = {
    som_ref: payload.som_ref as string | null | undefined,
    tree_ref: payload.tree_ref as string | null | undefined,
    observation_mode: payload.observation_mode as StepObservation["observation_mode"],
    gap_reasons: payload.gap_reasons as string[] | undefined,
    estimated_tokens: payload.estimated_tokens as number | undefined,
    observation_id: payload.observation_id as string | undefined,
    captured_monotonic_ms: payload.captured_monotonic_ms as number | undefined,
    frame_geometry: payload.frame_geometry as number[] | undefined,
  };
  if ("coordinate_actionable" in payload) {
    observation.coordinate_actionable = payload.coordinate_actionable as boolean | undefined;
  }
  if ("index_actionable" in payload) {
    observation.index_actionable = payload.index_actionable as boolean | undefined;
  }
  return observation;
}

const OBSERVATION_PRIORITY: Record<RoleCallRole, number> = {
  planner: 1,
  reviewer: 2,
  executor: 3,
};

/** Whether a focused-role packet replaces the current live observation. */
export function observationRoleWins(
  currentRole: RoleCallRole | null | undefined,
  sourceRole: RoleCallRole,
): boolean {
  return !currentRole || OBSERVATION_PRIORITY[sourceRole] > OBSERVATION_PRIORITY[currentRole];
}

/** Apply the same exact-envelope priority used by the persisted timeline. */
export function reconcileObservation(
  current: StepObservation | null | undefined,
  src: StepObservation | null | undefined,
  sourceHasPriority: boolean,
): StepObservation {
  const base: StepObservation = current ?? {
    som_ref: null,
    tree_ref: null,
    observation_mode: null,
    gap_reasons: [],
  };
  if (!src) return base;
  if (sourceHasPriority || !current) {
    return observationFromTracePayload(src as Record<string, unknown>);
  }

  const merged: StepObservation = { ...base };
  if (merged.som_ref == null) merged.som_ref = src.som_ref ?? null;
  if (merged.tree_ref == null) merged.tree_ref = src.tree_ref ?? null;
  if (merged.observation_mode == null) merged.observation_mode = src.observation_mode ?? null;
  if ((merged.gap_reasons ?? []).length === 0) merged.gap_reasons = src.gap_reasons ?? [];
  if (merged.estimated_tokens == null) merged.estimated_tokens = src.estimated_tokens;
  if (merged.observation_id == null) merged.observation_id = src.observation_id;
  if (merged.captured_monotonic_ms == null) merged.captured_monotonic_ms = src.captured_monotonic_ms;
  if (merged.frame_geometry == null) merged.frame_geometry = src.frame_geometry;
  if (merged.coordinate_actionable == null) merged.coordinate_actionable = src.coordinate_actionable;
  if (merged.index_actionable == null) merged.index_actionable = src.index_actionable;
  return merged;
}

/** Project a dispatched device-space action onto its exact recorded frame. */
export function projectActionToFrame(
  action: Action | null | undefined,
  frameGeometry: number[] | null | undefined,
  image: ImageGeometry,
): Action | null {
  if (!action || image.width <= 0 || image.height <= 0) return null;
  if (!frameGeometry || frameGeometry.length !== 2) return null;
  const [deviceWidth, deviceHeight] = frameGeometry;
  if (deviceWidth <= 0 || deviceHeight <= 0) return null;

  const scaleX = image.width / deviceWidth;
  const scaleY = image.height / deviceHeight;
  return {
    ...action,
    x: action.x == null ? action.x : action.x * scaleX,
    y: action.y == null ? action.y : action.y * scaleY,
    x2: action.x2 == null ? action.x2 : action.x2 * scaleX,
    y2: action.y2 == null ? action.y2 : action.y2 * scaleY,
  };
}
