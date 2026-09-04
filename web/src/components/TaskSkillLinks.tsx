import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getTaskSkillLinks } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function TaskSkillLinks({ taskId }: { taskId: string }) {
  const q = useQuery({
    queryKey: ["skill-links", taskId],
    queryFn: () => getTaskSkillLinks(taskId),
    enabled: !!taskId,
  });
  const data = q.data;
  if (!data) return null;
  if (data.skill_ids.length === 0 && data.pending.length === 0) return null;

  return (
    <Card className="border-border bg-bg-1">
      <CardHeader className="pb-2">
        <CardTitle className="font-mono text-[11px] uppercase tracking-wide text-text-mute">
          skills linked
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 font-mono text-xs">
        {data.skill_ids.length > 0 && (
          <div className="flex flex-wrap gap-2">
            <span className="text-text-mute">used</span>
            {data.skill_ids.map((id) => (
              <Link
                key={id}
                to={`/skills?id=${encodeURIComponent(id)}&tab=canonical`}
                className="text-cyan underline-offset-2 hover:underline"
              >
                {id}
              </Link>
            ))}
          </div>
        )}
        {data.pending.length > 0 && (
          <div className="flex flex-wrap gap-2">
            <span className="text-text-mute">pending</span>
            {data.pending.map((item) => (
              <Link
                key={item.id}
                to={`/skills?id=${encodeURIComponent(item.id)}&tab=pending`}
                className="text-amber underline-offset-2 hover:underline"
              >
                {item.id} ({item.status})
              </Link>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
