import { t } from "@/lib/locale";
import { useLocale } from "@/lib/useLocale";
import { useState } from "react";
import { ChevronRight, Image } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SomOverlay } from "@/components/SomOverlay";
import type { ObservationImage } from "@/lib/observationImage";

export function ObservationPanel({ taskId, image }: { taskId: string; image: ObservationImage | null }) {
  useLocale();
  // Remount for a different artifact/selection so load errors and aspect ratio
  // cannot leak from a previous historical image into the next observation.
  return <Card className="border-border bg-bg-1">
    <CardHeader className="pb-2">
      <CardTitle className="flex items-center gap-2 font-mono text-[11px] tracking-wide text-text-mute">
        <Image className="h-3.5 w-3.5" />{t("观测截图")}</CardTitle>
    </CardHeader>
    <CardContent>
      <p className="mb-3 text-xs text-text-mute">{t("已保存的任务画面，随观测记录更新。")}</p>
      {image?.call && <p className="mb-2 text-xs text-text-mute">
        {image.source === "model" ? t("模型交互画面") : t("任务观测")} · {image.call.role}
        {image.call.step_seq != null ? t(" · 步骤 {value0}", { value0: image.call.step_seq }) : ""}
      </p>}
      <details className="group mb-3 rounded border border-border">
        <summary className="flex cursor-pointer items-center gap-2 px-2 py-1 text-xs text-text-mute">
          <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />{t("使用说明")}</summary>
        <div className="space-y-2 border-t border-border p-2 text-xs leading-relaxed text-text-mute">
          <p>{t("开启“跟随最新步骤”可查看新记录的观测截图；点击时间轴步骤可查看历史画面。")}</p>
          <p>{t("点击模型输入中的图片可查看该轮模型收到的画面。没有保存图片时会显示不可用。")}</p>
        </div>
      </details>
      <ObservationScreen key={`${taskId}:${image?.selectionKey ?? "empty"}:${image?.artifactRef ?? ""}`} taskId={taskId} image={image} />
    </CardContent>
  </Card>;
}

function ObservationScreen({ taskId, image }: { taskId: string; image: ObservationImage | null }) {
  useLocale();
  const [aspect, setAspect] = useState(9 / 16);
  return <div className="relative mx-auto w-full max-w-[420px] rounded-[28px] border-2 border-border-hi bg-bg-2 p-3">
    <span aria-hidden className="absolute left-1/2 top-2 h-1 w-12 -translate-x-1/2 rounded-full bg-bg-3" />
    <div className="relative w-full overflow-hidden rounded-[20px] bg-black" style={{ aspectRatio: aspect }}>
      <div className="absolute inset-0 flex items-center justify-center">
        {image ? <SomOverlay somRef={image.artifactRef} taskId={taskId} action={image.action} frameGeometry={image.frameGeometry}
          onImageSize={({ width, height }) => { if (width > 0 && height > 0) setAspect(width / height); }} />
          : <p role="status" className="px-4 text-center text-xs text-text-mute">{t("当前步骤没有已保存的截图")}</p>}
      </div>
    </div>
    <span aria-hidden className="absolute bottom-2 left-1/2 h-1 w-10 -translate-x-1/2 rounded-full bg-bg-3" />
  </div>;
}
