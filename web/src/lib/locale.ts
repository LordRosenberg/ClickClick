import { english } from "./english.ts";

export type Locale = "zh-CN" | "en";
const storageKey = "clickclick-console-locale";
const listeners = new Set<() => void>();
function initialLocale(): Locale {
  if (typeof window === "undefined") return "zh-CN";
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved === "zh-CN" || saved === "en") return saved;
  } catch { /* Storage can be disabled. */ }
  return window.navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}
let locale = initialLocale();
export const getLocale = () => locale;
export function subscribeLocale(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function setLocale(value: Locale) {
  if (value === locale) return;
  locale = value;
  if (typeof document !== "undefined") document.documentElement.lang = value;
  try { if (typeof window !== "undefined") window.localStorage.setItem(storageKey, value); } catch { /* Optional preference. */ }
  for (const listener of listeners) listener();
}
export function translate(source: string, language: Locale, values: Record<string, unknown> = {}) {
  const template = language === "en" ? (english[source] ?? source) : source;
  // Replace once: braces within user/model content must remain literal.
  return template.replace(/\{(value\d+)\}/g, (match, key: string) =>
    Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match);
}
export function t(source: string, values?: Record<string, unknown>) {
  return translate(source, locale, values);
}
if (typeof document !== "undefined") document.documentElement.lang = locale;
