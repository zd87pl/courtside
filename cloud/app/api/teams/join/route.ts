import { z } from "zod";
import { ok, route } from "@/lib/api";
import { requireRole } from "@/lib/auth";
import { q, type Team } from "@/lib/db";

const Body = z.object({ code: z.string().min(4).max(12) });

export const POST = route(async (req) => {
  const player = await requireRole("player");
  const { code } = Body.parse(await req.json());
  const teams = await q<Team>(
    (s) => s`SELECT * FROM teams WHERE invite_code = ${code.toUpperCase().trim()}`,
  );
  if (!teams.length) throw new Error("no team found for that invite code");
  await q(
    (s) => s`INSERT INTO team_members (team_id, player_id) VALUES (${teams[0].id}, ${player.id})
             ON CONFLICT DO NOTHING`,
  );
  return ok({ teamId: teams[0].id, name: teams[0].name });
});
