import { useSyncExternalStore } from "react";
import { getLocale, setLocale, subscribeLocale } from "./locale";

export function useLocale() {
  const locale = useSyncExternalStore(subscribeLocale, getLocale, () => "zh-CN" as const);
  return { locale, setLocale };
}
