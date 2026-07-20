"use client";

/**
 * The cloud analysis flow, all client-orchestrated:
 * decode video in-browser -> motion-energy segmentation -> sample frames ->
 * per-clip /api/analyze (OpenRouter server-side) -> /api/sessions/finalize.
 * The video file itself never leaves the browser; only sampled JPEG frames do.
 */
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import {
  activityCurve,
  detectSegments,
  extractClip,
  fixedWindows,
  loadVideo,
  type Segment,
} from "@/lib/client/video";

const MAX_CLIPS = 6;

interface TeamOpt { id: string; name: string }
interface RosterOpt { id: string; name: string; team_id: string }

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(String((data as { error?: string }).error ?? `request failed (${res.status})`));
  return data as T;
}

export function Uploader({ role, teams, roster }: {
  role: "coach" | "player";
  teams: TeamOpt[];
  roster: RosterOpt[];
}) {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [teamId, setTeamId] = useState("");
  const [playerId, setPlayerId] = useState("");
  const [title, setTitle] = useState("");
  const [phase, setPhase] = useState<"idle" | "segmenting" | "analyzing" | "finalizing" | "failed">("idle");
  const [pct, setPct] = useState(0);
  const [log, setLog] = useState<string[]>([]);
  const say = (m: string) => setLog((l) => [...l.slice(-30), m]);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    try {
      setPhase("segmenting");
      setPct(4);
      say(`decoding ${file.name} in the browser (nothing uploads)`);
      const video = await loadVideo(file);
      const { times, scores } = await activityCurve(video, (f) => setPct(4 + Math.round(f * 20)));
      let segments: Segment[] = detectSegments(times, scores, video.duration);
      if (!segments.length) {
        say("auto-segmentation found nothing usable; using fixed 20s windows");
        segments = fixedWindows(video.duration);
      }
      segments = segments.slice(0, MAX_CLIPS);
      say(`${segments.length} rally clip(s) to analyze`);

      const { id: sessionId } = await post<{ id: string }>("/api/sessions", {
        title: title || file.name,
        teamId: teamId || null,
        playerId: playerId || null,
      });

      setPhase("analyzing");
      const clips: { analysis: unknown; thumbs: string[] }[] = [];
      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        say(`clip ${i + 1}/${segments.length}: ${seg.startS.toFixed(1)}-${seg.endS.toFixed(1)}s - extracting frames`);
        const clip = await extractClip(video, seg);
        say(`clip ${i + 1}/${segments.length}: analyzing ${clip.frames.length} frames via OpenRouter`);
        const analysis = await post("/api/analyze", {
          startS: seg.startS,
          endS: seg.endS,
          timestamps: clip.timestamps,
          frames: clip.frames,
        });
        clips.push({
          analysis,
          thumbs: clip.frames.filter((_, j) => j % Math.max(1, Math.floor(clip.frames.length / 3)) === 0).slice(0, 3),
        });
        setPct(24 + Math.round(((i + 1) / segments.length) * 60));
      }

      setPhase("finalizing");
      say("generating the coaching report");
      await post("/api/sessions/finalize", {
        sessionId,
        videoName: file.name,
        durationS: video.duration,
        clips,
      });
      setPct(100);
      router.push(`/s/${sessionId}`);
    } catch (err) {
      setPhase("failed");
      say(`failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const busy = phase !== "idle" && phase !== "failed";
  const teamPlayers = roster.filter((r) => !teamId || r.team_id === teamId);

  return (
    <>
      <div className="card">
        <h2>Analyze a match video</h2>
        <p className="hint">
          The video is decoded in your browser: rallies are found locally and only sampled frames are
          sent for analysis. First {MAX_CLIPS} rallies in this PoC.
        </p>
        <form onSubmit={run}>
          <label className="f">Video file (mp4/mov)</label>
          <input ref={fileRef} type="file" accept="video/*" required disabled={busy} />
          <div className="row">
            <div>
              <label className="f">Session title</label>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Tuesday practice" disabled={busy} />
            </div>
            <div>
              <label className="f">Team (optional)</label>
              <select value={teamId} onChange={(e) => setTeamId(e.target.value)} disabled={busy}>
                <option value="">-</option>
                {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </div>
            {role === "coach" && (
              <div>
                <label className="f">Player (optional)</label>
                <select value={playerId} onChange={(e) => setPlayerId(e.target.value)} disabled={busy}>
                  <option value="">-</option>
                  {teamPlayers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
            )}
          </div>
          <div style={{ marginTop: 14 }}>
            <button className="btn-pri" type="submit" disabled={busy}>
              {busy ? "Working..." : "Analyze"}
            </button>
          </div>
        </form>
      </div>

      {phase !== "idle" && (
        <div className="card">
          <div className="progbar"><div style={{ width: `${pct}%` }} /></div>
          <p className="hint" style={{ margin: "10px 0 0" }}>
            {phase === "failed" ? "Failed - see log below." : `${phase} · ${pct}%`}
          </p>
          <pre style={{ fontSize: 12, whiteSpace: "pre-wrap", color: "var(--ink-2)", margin: "10px 0 0" }}>
            {log.join("\n")}
          </pre>
        </div>
      )}
    </>
  );
}
