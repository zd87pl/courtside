import Link from "next/link";
import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { CreateTeamForm, JoinTeamForm } from "@/components/forms";
import { getUser } from "@/lib/auth";
import { q, type SessionRow, type Team } from "@/lib/db";

export const dynamic = "force-dynamic";

interface SessionListRow extends SessionRow {
  player_name: string | null;
  team_name: string | null;
}

export default async function Dashboard() {
  const user = await getUser();
  if (!user) redirect("/login");

  const teams =
    user.role === "coach"
      ? await q<Team & { members: number }>(
          (s) => s`SELECT t.*, (SELECT count(*)::int FROM team_members m WHERE m.team_id = t.id) AS members
                   FROM teams t WHERE t.coach_id = ${user.id} ORDER BY t.created_at DESC`,
        )
      : await q<Team>(
          (s) => s`SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id
                   WHERE m.player_id = ${user.id} ORDER BY t.created_at DESC`,
        );

  const sessions =
    user.role === "coach"
      ? await q<SessionListRow>(
          (s) => s`SELECT se.*, p.name AS player_name, t.name AS team_name
                   FROM sessions se
                   LEFT JOIN users p ON p.id = se.player_id
                   LEFT JOIN teams t ON t.id = se.team_id
                   WHERE se.owner_id = ${user.id}
                      OR se.team_id IN (SELECT id FROM teams WHERE coach_id = ${user.id})
                   ORDER BY se.created_at DESC LIMIT 50`,
        )
      : await q<SessionListRow>(
          (s) => s`SELECT se.*, p.name AS player_name, t.name AS team_name
                   FROM sessions se
                   LEFT JOIN users p ON p.id = se.player_id
                   LEFT JOIN teams t ON t.id = se.team_id
                   WHERE se.owner_id = ${user.id} OR se.player_id = ${user.id}
                   ORDER BY se.created_at DESC LIMIT 50`,
        );

  return (
    <Shell user={user} title="Dashboard">
      <div className="tiles">
        <div className="tile"><b className="tnum">{sessions.length}</b><span>sessions</span></div>
        <div className="tile"><b className="tnum">{teams.length}</b><span>{user.role === "coach" ? "teams" : "teams joined"}</span></div>
        <div className="tile"><b className="tnum">
          {sessions.filter((s) => s.status === "complete").length}</b><span>reports ready</span></div>
      </div>

      <div className="card">
        <h2>{user.role === "coach" ? "Your teams" : "Your team"}</h2>
        {user.role === "coach" ? (
          <>
            <p className="hint">Create a team per school or squad; share the invite code with players.</p>
            {teams.map((t) => (
              <div key={t.id} className="row" style={{ alignItems: "center", padding: "8px 0", borderBottom: "1px solid var(--border-2)" }}>
                <div>
                  <b>{t.name}</b>{" "}
                  {t.school && <span className="crumb">{t.school}</span>}
                  <div className="hint" style={{ margin: 0 }}>
                    {(t as Team & { members: number }).members ?? 0} players
                  </div>
                </div>
                <div style={{ flex: "0 0 auto" }}>
                  invite code <span className="tag" style={{ fontSize: 13 }}>{t.invite_code}</span>
                </div>
              </div>
            ))}
            <div style={{ marginTop: 12 }}>
              <CreateTeamForm />
            </div>
          </>
        ) : (
          <>
            {teams.length ? (
              teams.map((t) => (
                <p key={t.id}>
                  <b>{t.name}</b> {t.school && <span className="crumb">{t.school}</span>}
                </p>
              ))
            ) : (
              <p className="hint">You haven&apos;t joined a team yet.</p>
            )}
            <JoinTeamForm />
          </>
        )}
      </div>

      <div className="kicker">Sessions</div>
      {sessions.length === 0 ? (
        <div className="card">
          <p className="hint" style={{ margin: 0 }}>
            No sessions yet - <Link href="/upload">run your first analysis</Link>.
          </p>
        </div>
      ) : (
        <div className="slist">
          {sessions.map((s) => (
            <Link key={s.id} href={`/s/${s.id}`}>
              <span>
                <b>{s.title}</b>
                <span className="m" style={{ marginLeft: 8 }}>
                  {s.player_name ?? ""} {s.team_name ? `· ${s.team_name}` : ""}
                </span>
              </span>
              <span className="m">
                {s.status !== "complete" && <span className="tag">{s.status}</span>}{" "}
                {new Date(s.created_at).toISOString().slice(0, 10)}
              </span>
            </Link>
          ))}
        </div>
      )}
    </Shell>
  );
}
