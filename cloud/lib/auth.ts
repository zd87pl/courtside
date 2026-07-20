/**
 * Minimal, transparent credentials auth for the PoC SaaS:
 * scrypt password hashes + httpOnly cookie whose random token is stored
 * hashed in Postgres (auth_sessions). No third-party auth dependency.
 */
import { createHash, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { q, type Role, type User } from "./db";

const COOKIE = "cs_session";
const SESSION_DAYS = 30;

export function hashPassword(password: string): string {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(password, salt, 64).toString("hex");
  return `s1:${salt}:${hash}`;
}

export function verifyPassword(password: string, stored: string): boolean {
  const [v, salt, hash] = stored.split(":");
  if (v !== "s1" || !salt || !hash) return false;
  const got = scryptSync(password, salt, 64);
  const want = Buffer.from(hash, "hex");
  return got.length === want.length && timingSafeEqual(got, want);
}

const tokenHash = (t: string) => createHash("sha256").update(t).digest("hex");

export async function createSession(userId: string): Promise<void> {
  const token = randomBytes(32).toString("hex");
  const expires = new Date(Date.now() + SESSION_DAYS * 864e5);
  await q(
    (s) => s`INSERT INTO auth_sessions (token_hash, user_id, expires_at)
             VALUES (${tokenHash(token)}, ${userId}, ${expires.toISOString()})`,
  );
  (await cookies()).set(COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    expires,
  });
}

export async function destroySession(): Promise<void> {
  const jar = await cookies();
  const token = jar.get(COOKIE)?.value;
  if (token) {
    await q((s) => s`DELETE FROM auth_sessions WHERE token_hash = ${tokenHash(token)}`);
  }
  jar.delete(COOKIE);
}

export async function getUser(): Promise<User | null> {
  const token = (await cookies()).get(COOKIE)?.value;
  if (!token) return null;
  const rows = await q<User>(
    (s) => s`SELECT u.* FROM auth_sessions a JOIN users u ON u.id = a.user_id
             WHERE a.token_hash = ${tokenHash(token)} AND a.expires_at > now()`,
  );
  return rows[0] ?? null;
}

export async function requireUser(): Promise<User> {
  const u = await getUser();
  if (!u) throw new AuthError();
  return u;
}

export async function requireRole(role: Role): Promise<User> {
  const u = await requireUser();
  if (u.role !== role) throw new AuthError(`requires ${role} role`);
  return u;
}

export class AuthError extends Error {
  constructor(msg = "not signed in") {
    super(msg);
  }
}

export function inviteCode(): string {
  // short, human-shareable, unambiguous alphabet
  const abc = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
  return Array.from(randomBytes(6), (b) => abc[b % abc.length]).join("");
}
