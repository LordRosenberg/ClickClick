export type PreviewLocale = "en" | "zh-CN";
const KEY = "clickclick.preview.locale";
type StorageAccess = () => Pick<Storage, "getItem" | "setItem">;

export function loadPreviewLocale(storage: StorageAccess = () => localStorage): PreviewLocale {
  try { return storage().getItem(KEY) === "zh-CN" ? "zh-CN" : "en"; }
  catch { return "en"; }
}

export function savePreviewLocale(locale: PreviewLocale, storage: StorageAccess = () => localStorage): void {
  try { storage().setItem(KEY, locale); } catch { /* Preference storage is optional. */ }
}
