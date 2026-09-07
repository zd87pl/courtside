import { z } from "zod";
import { ok, route } from "@/lib/api";
import { requireUser } from "@/lib/auth";
import { q, type SessionRow } from "@/lib/db";

const Body = z.object({
  title: z.string().min(1).max(200),
  teamId: z.string().uuid().nullish(),
  playerId: z.string().uuid().nullish(),
});

export const POST = route(async (req) => {
  const user = await requireUser();
  const { title, teamId, playerId } = Body.parse(await req.json());
  const selectedPlayer = playerId ?? (user.role === "player" ? user.id : null);
  if (user.role === "player" && selectedPlayer !== user.id) {
    return ok({ error: "players can only create their own sessions" }, 403);
  }
  if (teamId) {
    const allowed = await q<{ id: string }>(
      (s) => s`SELECT id FROM teams WHERE id = ${teamId} AND
        (coach_id = ${user.id} OR EXISTS (
          SELECT 1 FROM team_members WHERE team_id = ${teamId} AND player_id = ${user.id}))`,
    );
    if (!allowed.length) return ok({ error: "team not found" }, 404);
  }
  if (selectedPlayer && selectedPlayer !== user.id) {
    if (user.role !== "coach" || !teamId) return ok({ error: "select a team you coach" }, 403);
    const member = await q<{ id: string }>(
      (s) => s`SELECT t.id FROM teams t JOIN team_members m ON m.team_id = t.id
        WHERE t.id = ${teamId} AND t.coach_id = ${user.id} AND m.player_id = ${selectedPlayer}`,
    );
    if (!member.length) return ok({ error: "player not found in your team" }, 404);
  }
  const rows = await q<SessionRow>(
    (s) => s`INSERT INTO sessions (owner_id, player_id, team_id, title, status)
             VALUES (${user.id}, ${selectedPlayer},
                     ${teamId ?? null}, ${title}, 'processing')
             RETURNING id`,
  );
  return ok({ id: rows[0].id }, 201);
});
