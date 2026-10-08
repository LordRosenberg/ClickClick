import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

type Notice = { supported: boolean; available_version?: string; error?: string };

export function DesktopUpdateNotice() {
  useLocale();
  const [notice, setNotice] = useState<Notice>();
  const location = useLocation();
  useEffect(() => {
    let alive = true;
    async function refresh() {
      try {
        const response = await fetch("/api/desktop/update-notice");
        if (!response.ok) return;
        const result = await response.json();
        if (alive) setNotice(result);
      } catch { /* Console reconnects normally; this notice never starts updates. */ }
    }
    void refresh();
    const timer = setInterval(refresh, 60000);
    return () => { alive = false; clearInterval(timer); };
  }, [location.pathname]);
  if (!notice?.supported || location.pathname === "/setup" || (!notice.available_version && !notice.error)) return null;
  return <div role="status" className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded border border-cyan/40 bg-bg-2 px-4 py-3 text-sm">
    <span>{notice.available_version ? t("ClickClick 新版 {value0} 已发布", { value0: notice.available_version }) : t("ClickClick 更新检查或升级未完成")}</span>
    <Link className="text-cyan underline" to="/setup">{t("查看更新并确认升级")}</Link>
  </div>;
}
