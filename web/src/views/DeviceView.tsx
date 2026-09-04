import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { deviceLabel, getHealth, getScrcpyHint, listDevices } from "@/api/client";

export function DeviceView() {
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: listDevices,
    refetchInterval: 3000,
  });
  const health = useQuery({ queryKey: ["health"], queryFn: getHealth, refetchInterval: 5000 });
  const scrcpy = useQuery({ queryKey: ["scrcpy"], queryFn: getScrcpyHint });

  const rows = devices.data ?? [];
  const mode = (health.data?.driver as { mode?: string } | undefined)?.mode;

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px] font-mono">
      <Card className="border-border bg-bg-1">
        <CardHeader className="pb-2">
          <div className="flex items-center gap-2">
            <CardTitle className="font-mono text-xs uppercase tracking-wider text-text-mute">
              <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-cyan" />
              devices
            </CardTitle>
            <Badge variant="outline" className="border-border bg-bg-2 text-text-mute">
              {rows.length} online
            </Badge>
            {mode ? (
              <Badge variant="outline" className="border-border bg-bg-2 text-text-mute">
                {mode}
              </Badge>
            ) : null}
          </div>
          <CardDescription className="font-mono text-[10px] uppercase tracking-wider text-text-mute">
            binding key = hub/serial on remote · model / market name display-only
          </CardDescription>
        </CardHeader>
        <CardContent>
          {devices.isLoading && (
            <div className="text-xs text-text-mute">loading…</div>
          )}
          {devices.isError && (
            <div className="text-xs text-err">— backend unreachable</div>
          )}
          {!devices.isLoading && rows.length === 0 && (
            <div className="text-xs text-text-mute">
              — no authorized devices online (`adb devices`)
            </div>
          )}
          {rows.length > 0 && (
            <div className="overflow-x-auto rounded border border-border">
              <table className="w-full text-left text-[11px]">
                <thead className="bg-bg-2 text-[10px] uppercase tracking-wide text-text-mute">
                  <tr>
                    <th className="px-2 py-1.5 font-normal">display</th>
                    <th className="px-2 py-1.5 font-normal">hub</th>
                    <th className="px-2 py-1.5 font-normal">serial</th>
                    <th className="px-2 py-1.5 font-normal">model</th>
                    <th className="px-2 py-1.5 font-normal">status</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((d) => (
                    <tr key={d.key} className="border-t border-border">
                      <td className="px-2 py-1.5 text-text">{deviceLabel(d)}</td>
                      <td className="px-2 py-1.5 text-text-mute">{d.driver_id}</td>
                      <td className="px-2 py-1.5 text-cyan">{d.serial}</td>
                      <td className="px-2 py-1.5 text-text-mute">{d.model || "—"}</td>
                      <td className="px-2 py-1.5">
                        {d.busy && d.busy_task_id ? (
                          <Link
                            to={`/tasks/${d.busy_task_id}`}
                            className="text-amber underline-offset-2 hover:underline"
                          >
                            busy → {d.busy_task_id.slice(0, 8)}
                          </Link>
                        ) : (
                          <span className="text-neon">idle</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="border-border bg-bg-1">
        <CardHeader className="pb-2">
          <CardTitle className="font-mono text-xs uppercase tracking-wider text-text-mute">
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-amber" />
            scrcpy mirror
          </CardTitle>
          <CardDescription className="font-mono text-[10px] uppercase tracking-wider text-text-mute">
            external — not required for console debugging
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-[12px]">
          <p className="text-text-mute">
            Console 的任务管理与调试不依赖实时投屏。如需观察设备画面，可在终端单独运行 scrcpy，或使用任务详情右栏 Live（解码完善中）。
          </p>
          {scrcpy.data && (
            <div className="rounded border border-border bg-bg-2 p-2 text-[11px] text-cyan">
              {scrcpy.data.command}
            </div>
          )}
          <p className="text-[10px] uppercase tracking-wider text-text-mute">
            {scrcpy.data?.note}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
