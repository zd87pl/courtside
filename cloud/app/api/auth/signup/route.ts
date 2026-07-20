import { z } from "zod";
import { ok, route } from "@/lib/api";
import { createSession, hashPassword } from "@/lib/auth";
import { q, type User } from "@/lib/db";

const Body = z.object({
  email: z.string().email().max(200),
  password: z.string().min(8).max(200),
  name: z.string().min(1).max(120),
  role: z.enum(["coach", "player"]),
});

export const POST = route(async (req) => {
  const { email, password, name, role } = Body.parse(await req.json());
  const existing = await q<User>((s) => s`SELECT id FROM users WHERE email = ${email.toLowerCase()}`);
  if (existing.length) throw new Error("an account with this email already exists");
  const rows = await q<User>(
    (s) => s`INSERT INTO users (email, name, role, password_hash)
             VALUES (${email.toLowerCase()}, ${name}, ${role}, ${hashPassword(password)})
             RETURNING *`,
  );
  await createSession(rows[0].id);
  return ok({ id: rows[0].id, role: rows[0].role }, 201);
});
