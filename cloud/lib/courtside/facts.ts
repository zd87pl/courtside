/** Session facts + flag normalization, ported from courtside/report.py + schema.py. */
import type { ClipAnalysisT } from "./schema";

const ALIASES: Record<string, string> = {
  late_prep: "late_preparation",
  preparation_late: "late_preparation",
  prep_late: "late_preparation",
  no_splitstep: "no_split_step",
  split_step_missing: "no_split_step",
  missing_split_step: "no_split_step",
  low_toss: "low_ball_toss",
  toss_low: "low_ball_toss",
  predictable_serve: "serve_placement_predictable",
};

export function normalizeFlagCode(code: string): string {
  const c = code.trim().toLowerCase().replace(/[\s-]+/g, "_");
  return ALIASES[c] ?? c;
}

function topN(counter: Map<string, number>, n: number): Record<string, number> {
  return Object.fromEntries(
    [...counter.entries()].sort((a, b) => b[1] - a[1]).slice(0, n),
  );
}

export function computeSessionFacts(analyses: ClipAnalysisT[]) {
  let near = 0, far = 0, total = 0;
  const mix = new Map<string, number>();
  const tech = new Map<string, number>();
  const tact = new Map<string, number>();
  const conf = new Map<string, number>();
  for (const a of analyses) {
    conf.set(a.confidence, (conf.get(a.confidence) ?? 0) + 1);
    for (const s of a.strokes) {
      total++;
      if (s.player === "near") near++;
      else if (s.player === "far") far++;
      mix.set(s.stroke, (mix.get(s.stroke) ?? 0) + 1);
      for (const f of s.technique_flags) {
        const c = normalizeFlagCode(f.code);
        tech.set(c, (tech.get(c) ?? 0) + 1);
      }
      for (const f of s.tactical_flags) {
        const c = normalizeFlagCode(f.code);
        tact.set(c, (tact.get(c) ?? 0) + 1);
      }
    }
  }
  return {
    clips_analyzed: analyses.length,
    total_strokes: total,
    player_split: { near, far, unknown: total - near - far },
    stroke_mix: Object.fromEntries(mix),
    top_technique_flags: topN(tech, 8),
    top_tactical_flags: topN(tact, 8),
    confidence_distribution: Object.fromEntries(conf),
  };
}

export type SessionFacts = ReturnType<typeof computeSessionFacts>;
