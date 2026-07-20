import { z } from "zod";
import { ok, route } from "@/lib/api";
import { createSession, verifyPassword } from "@/lib/auth";
import { q, type User } from "@/lib/db";

const Body = z.object({ email: z.string().email(), password: z.string() });

export const POST = route(async (req) => {
  const { email, password } = Body.parse(await req.json());
  const rows = await q<User>((s) => s`SELECT * FROM users WHERE email = ${email.toLowerCase()}`);
  const user = rows[0];
  if (!user || !verifyPassword(password, user.password_hash)) {
    throw new Error("invalid email or password");
  }
  await createSession(user.id);
  return ok({ id: user.id, role: user.role });
});
