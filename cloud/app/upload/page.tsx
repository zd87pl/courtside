import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { Uploader } from "@/components/Uploader";
import { getUser } from "@/lib/auth";
import { q, type Team } from "@/lib/db";

export const dynamic = "force-dynamic";

export default async function UploadPage() {
  const user = await getUser();
  if (!user) redirect("/login");

  const teams =
    user.role === "coach"
      ? await q<Team>((s) => s`SELECT * FROM teams WHERE coach_id = ${user.id} ORDER BY name`)
      : await q<Team>(
          (s) => s`SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id
                   WHERE m.player_id = ${user.id} ORDER BY t.name`,
        );
  const roster =
    user.role === "coach"
      ? await q<{ id: string; name: string; team_id: string }>(
          (s) => s`SELECT u.id, u.name, m.team_id FROM users u
                   JOIN team_members m ON m.player_id = u.id
                   WHERE m.team_id IN (SELECT id FROM teams WHERE coach_id = ${user.id})
                   ORDER BY u.name`,
        )
      : [];

  return (
    <Shell user={user} title="New analysis">
      <Uploader
        role={user.role}
        teams={teams.map((t) => ({ id: t.id, name: t.name }))}
        roster={roster}
      />
    </Shell>
  );
}
