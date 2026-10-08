import type { Action, RoleCall } from "../api/types.ts";

export type ObservationImage = {
  artifactRef: string;
  selectionKey: string;
  call: RoleCall | null;
  action: Action | null;
  frameGeometry?: number[];
  source: "model" | "observation";
};

/** Pure artifact selection: never queries or captures a device. */
export function resolveObservationImage(
  calls: RoleCall[],
  selected: RoleCall | null,
  following: boolean,
  visual?: { artifactRef: string; rowKey: string } | null,
): ObservationImage | null {
  if (visual) return {
    artifactRef: visual.artifactRef, selectionKey: visual.rowKey,
    call: selected, action: null, source: "model",
  };
  const ref = (call: RoleCall | null) => call?.observation?.model_image_ref || call?.observation?.som_ref;
  const owner = ref(selected) ? selected : following ? [...calls].reverse().find(call => ref(call)) ?? null : null;
  const artifactRef = ref(owner);
  if (!owner || !artifactRef) return null;
  return {
    artifactRef, selectionKey: owner.call_key, call: owner, source: "observation",
    action: owner.role === "executor" ? owner.executor?.action ?? null : null,
    frameGeometry: owner.observation?.frame_geometry,
  };
}
