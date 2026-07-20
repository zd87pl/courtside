import { z } from "zod";
import { ok, route } from "@/lib/api";
import { requireUser } from "@/lib/auth";
import { generate, extractJson } from "@/lib/courtside/openrouter";
import { clipPrompt } from "@/lib/courtside/prompts";
import { CLIP_JSON_SCHEMA, ClipAnalysis } from "@/lib/courtside/schema";

export const maxDuration = 60;

const Body = z.object({
  startS: z.number().min(0),
  endS: z.number().min(0),
  timestamps: z.array(z.number()).min(1).max(16),
  frames: z.array(z.string().startsWith("data:image/jpeg;base64,")).min(1).max(16),
});

/** One clip in, one validated ClipAnalysis out - with the single repair retry. */
export const POST = route(async (req) => {
  await requireUser();
  const { startS, endS, timestamps, frames } = Body.parse(await req.json());
  const prompt = clipPrompt(frames.length, startS, endS, timestamps);

  const attempt = async (p: string) => {
    const text = await generate(p, { images: frames, jsonSchema: CLIP_JSON_SCHEMA, maxTokens: 1400 });
    return ClipAnalysis.parse(extractJson(text));
  };

  let analysis;
  try {
    analysis = await attempt(prompt);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    analysis = await attempt(
      `${prompt}\n\nYour previous output was invalid (${msg.slice(0, 200)}). ` +
        "Look at the frames again and return ONLY the corrected JSON object, nothing else.",
    );
  }
  // trust the pipeline's clip boundaries; clamp stroke times into the window
  analysis.start_s = startS;
  analysis.end_s = endS;
  for (const s of analysis.strokes) s.t_s = Math.min(Math.max(s.t_s, startS), endS);
  return ok(analysis);
});
