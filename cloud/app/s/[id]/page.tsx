import { notFound, redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getUser } from "@/lib/auth";
import type { SessionFacts } from "@/lib/courtside/facts";
import type { ClipAnalysisT, FlagT, StrokeT } from "@/lib/courtside/schema";
import { q, type SessionRow } from "@/lib/db";
import { mdToHtml } from "@/lib/md";

export const dynamic = "force-dynamic";

interface CloudDoc {
  video?: string;
  model?: string;
  facts?: SessionFacts;
  clips?: { index: number; analysis: ClipAnalysisT; thumbs?: string[] }[];
}

const SEV: Record<string, { color: string; label: string }> = {
  high: { color: "#e5484d", label: "critical" },
  medium: { color: "#f5a623", label: "warning" },
  low: { color: "#8a8f98", label: "minor" },
};

function flaggedMoments(doc: CloudDoc) {
  const out: { flag: FlagT; stroke: StrokeT; kind: string; thumb?: string }[] = [];
  for (const c of doc.clips ?? []) {
    for (const s of c.analysis.strokes) {
      for (const [kind, flags] of [["technique", s.technique_flags], ["tactical", s.tactical_flags]] as const) {
        for (const f of flags) {
          if (f.severity !== "low") out.push({ flag: f, stroke: s, kind, thumb: c.thumbs?.[1] ?? c.thumbs?.[0] });
        }
      }
    }
  }
  const rank = { high: 3, medium: 2, low: 1 } as const;
  return out.sort((a, b) => rank[b.flag.severity] - rank[a.flag.severity]).slice(0, 12);
}

export default async function SessionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const user = await getUser();
  if (!user) redirect("/login");

  const rows = await q<SessionRow & { player_name: string | null; team_name: string | null; coach_id: string | null }>(
    (s) => s`SELECT se.*, p.name AS player_name, t.name AS team_name, t.coach_id
             FROM sessions se
             LEFT JOIN users p ON p.id = se.player_id
             LEFT JOIN teams t ON t.id = se.team_id
             WHERE se.id = ${id}`,
  );
  const sess = rows[0];
  if (!sess) notFound();
  const allowed =
    sess.owner_id === user.id || sess.player_id === user.id || sess.coach_id === user.id;
  if (!allowed) notFound();

  const doc = (sess.doc ?? {}) as CloudDoc;
  const facts = doc.facts;
  const moments = flaggedMoments(doc);

  return (
    <Shell user={user} title={sess.title} crumb="session">
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
        <span className="pill">cloud analysis · {doc.model ?? "OpenRouter"}</span>
        {sess.player_name && <span className="pill">player: {sess.player_name}</span>}
        {sess.team_name && <span className="pill">team: {sess.team_name}</span>}
        {sess.status !== "complete" && <span className="tag">{sess.status}</span>}
      </div>

      {facts && (
        <div className="tiles">
          <div className="tile"><b className="tnum">{facts.total_strokes}</b><span>strokes</span></div>
          <div className="tile"><b className="tnum">{facts.clips_analyzed}</b><span>rallies</span></div>
          <div className="tile"><b className="tnum">{facts.player_split.near} / {facts.player_split.far}</b><span>near / far</span></div>
          <div className="tile"><b className="tnum">
            {Object.values(facts.top_technique_flags).reduce((a, b) => a + b, 0) +
              Object.values(facts.top_tactical_flags).reduce((a, b) => a + b, 0)}
          </b><span>coaching flags</span></div>
        </div>
      )}

      {moments.length > 0 && (
        <>
          <div className="kicker">Flagged moments</div>
          <div className="mgrid" style={{ marginBottom: 16 }}>
            {moments.map((m, i) => (
              <div className="mcard" key={i}>
                {m.thumb ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={m.thumb} alt="moment" />
                ) : (
                  <div style={{ aspectRatio: "16/9", background: "#000" }} />
                )}
                <div className="b">
                  <span className="chip" style={{ background: SEV[m.flag.severity].color }}>
                    {SEV[m.flag.severity].label}
                  </span>
                  <span className="code">{m.flag.code}</span>
                  <div className="mm">
                    {m.kind} · {m.stroke.stroke} ({m.stroke.player}) · t={m.stroke.t_s.toFixed(1)}s
                  </div>
                  <div className="ev">{m.flag.evidence}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {sess.report_md && (
        <>
          <div className="kicker">Coaching report</div>
          <div className="card rep" dangerouslySetInnerHTML={{ __html: mdToHtml(sess.report_md) }} />
        </>
      )}
    </Shell>
  );
}
