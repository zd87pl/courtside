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
  const rows = await q<SessionRow>(
    (s) => s`INSERT INTO sessions (owner_id, player_id, team_id, title, status)
             VALUES (${user.id}, ${playerId ?? (user.role === "player" ? user.id : null)},
                     ${teamId ?? null}, ${title}, 'processing')
             RETURNING id`,
  );
  return ok({ id: rows[0].id }, 201);
});
