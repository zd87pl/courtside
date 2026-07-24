import { z } from "zod";
import { ok, route } from "@/lib/api";
import { requireUser } from "@/lib/auth";
import { computeSessionFacts } from "@/lib/courtside/facts";
import { generate, stripThink } from "@/lib/courtside/openrouter";
import { LIMITATIONS_MD, reportPrompt } from "@/lib/courtside/prompts";
import { ClipAnalysis } from "@/lib/courtside/schema";
import { q } from "@/lib/db";

export const maxDuration = 60;

const Body = z.object({
  sessionId: z.string().uuid(),
  videoName: z.string().max(300).default("session"),
  durationS: z.number().min(0).default(0),
  clips: z
    .array(
      z.object({
        analysis: ClipAnalysis,
        thumbs: z.array(z.string().startsWith("data:image/jpeg;base64,")).max(4).default([]),
      }),
    )
    .min(1)
    .max(24),
});

/** Aggregate facts, generate the report, persist the session envelope. */
export const POST = route(async (req) => {
  const user = await requireUser();
  const { sessionId, videoName, durationS, clips } = Body.parse(await req.json());

  const owned = await q<{ id: string }>(
    (s) => s`SELECT id FROM sessions WHERE id = ${sessionId} AND owner_id = ${user.id}`,
  );
  if (!owned.length) throw new Error("session not found (or not yours)");

  const analyses = clips.map((c) => c.analysis);
  const facts = computeSessionFacts(analyses);

  let reportMd: string;
  try {
    const raw = await generate(reportPrompt(analyses, facts), { maxTokens: 2400, temperature: 0.4 });
    reportMd = stripThink(raw) + "\n" + LIMITATIONS_MD;
  } catch {
    reportMd = "# Session Analysis\n\nReport generation failed - the per-clip analyses below are intact.\n" + LIMITATIONS_MD;
  }

  const doc = {
    courtside_cloud: true,
    video: videoName,
    video_duration_s: durationS,
    backend: "openrouter",
    model: process.env.OPENROUTER_MODEL || "qwen/qwen2.5-vl-72b-instruct",
    created_at: new Date().toISOString(),
    facts,
    clips: clips.map((c, i) => ({ index: i, status: "ok", analysis: c.analysis, thumbs: c.thumbs })),
  };

  await q(
    (s) => s`UPDATE sessions SET status = 'complete', doc = ${JSON.stringify(doc)}::jsonb,
             report_md = ${reportMd} WHERE id = ${sessionId}`,
  );
  return ok({ ok: true });
});
