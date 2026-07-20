import { z } from "zod";
import { ok, route } from "@/lib/api";
import { inviteCode, requireRole } from "@/lib/auth";
import { q, type Team } from "@/lib/db";

const Body = z.object({ name: z.string().min(1).max(120), school: z.string().max(120).default("") });

export const POST = route(async (req) => {
  const coach = await requireRole("coach");
  const { name, school } = Body.parse(await req.json());
  const rows = await q<Team>(
    (s) => s`INSERT INTO teams (name, school, coach_id, invite_code)
             VALUES (${name}, ${school}, ${coach.id}, ${inviteCode()})
             RETURNING *`,
  );
  return ok(rows[0], 201);
});
