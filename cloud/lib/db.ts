/**
 * Postgres via Neon's serverless driver (works on Vercel with the Neon
 * integration: DATABASE_URL / POSTGRES_URL are auto-injected).
 *
 * Schema is bootstrapped idempotently on first use (CREATE TABLE IF NOT
 * EXISTS) so "point Vercel at the repo + add the Neon integration" is the
 * entire setup - no migration tooling required for the PoC.
 */
import { neon } from "@neondatabase/serverless";

export type Role = "coach" | "player";

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
  password_hash: string;
  created_at: string;
}

export interface Team {
  id: string;
  name: string;
  school: string;
  coach_id: string;
  invite_code: string;
  created_at: string;
}

export interface SessionRow {
  id: string;
  owner_id: string;
  player_id: string | null;
  team_id: string | null;
  title: string;
  status: "processing" | "complete" | "failed";
  doc: unknown; // the courtside session envelope (facts, clips, moments-lite)
  report_md: string | null;
  created_at: string;
}

function url(): string {
  const u = process.env.DATABASE_URL || process.env.POSTGRES_URL;
  if (!u) throw new Error("DATABASE_URL is not set - add the Neon (Postgres) integration in Vercel");
  return u;
}

// One driver instance per lambda; Neon's HTTP driver is stateless per query.
let _sql: ReturnType<typeof neon> | null = null;
let _ready: Promise<void> | null = null;

export function sql(): ReturnType<typeof neon> {
  if (!_sql) _sql = neon(url());
  return _sql;
}

export async function ensureSchema(): Promise<void> {
  if (!_ready) {
    const s = sql();
    _ready = (async () => {
      await s`CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('coach','player')),
        password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )`;
      await s`CREATE TABLE IF NOT EXISTS auth_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at TIMESTAMPTZ NOT NULL
      )`;
      await s`CREATE TABLE IF NOT EXISTS teams (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name TEXT NOT NULL,
        school TEXT NOT NULL DEFAULT '',
        coach_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        invite_code TEXT UNIQUE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )`;
      await s`CREATE TABLE IF NOT EXISTS team_members (
        team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        player_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (team_id, player_id)
      )`;
      await s`CREATE TABLE IF NOT EXISTS sessions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        player_id UUID REFERENCES users(id) ON DELETE SET NULL,
        team_id UUID REFERENCES teams(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','complete','failed')),
        doc JSONB,
        report_md TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )`;
      await s`CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id, created_at DESC)`;
      await s`CREATE INDEX IF NOT EXISTS idx_sessions_team ON sessions(team_id, created_at DESC)`;
      await s`CREATE INDEX IF NOT EXISTS idx_members_player ON team_members(player_id)`;
    })().catch((error) => { _ready = null; throw error; });
  }
  return _ready;
}

/** Query helper: schema-ensured, returns rows of T. */
export async function q<T>(run: (s: ReturnType<typeof neon>) => Promise<unknown>): Promise<T[]> {
  await ensureSchema();
  return (await run(sql())) as T[];
}
