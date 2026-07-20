/**
 * The clip-analysis contract, ported 1:1 from courtside/schema.py so prompts
 * and outputs stay interchangeable between the local demo and the cloud app.
 */
import { z } from "zod";

export const Severity = z.enum(["low", "medium", "high"]);
export const Confidence = z.enum(["low", "medium", "high"]);
export const Player = z.enum(["near", "far", "unknown"]);
export const StrokeType = z.enum([
  "serve", "forehand", "backhand", "forehand_volley", "backhand_volley",
  "overhead", "slice", "drop_shot", "lob", "return", "unknown",
]);

export const Flag = z.object({
  code: z.string(),
  severity: Severity,
  evidence: z.string().max(400),
});

export const Stroke = z.object({
  t_s: z.number(),
  player: Player,
  stroke: StrokeType,
  technique_flags: z.array(Flag).default([]),
  tactical_flags: z.array(Flag).default([]),
});

export const ClipAnalysis = z.object({
  start_s: z.number(),
  end_s: z.number(),
  strokes: z.array(Stroke).default([]),
  rally_summary: z.string().max(600),
  confidence: Confidence,
  notes: z.string().max(400).default(""),
});

export type FlagT = z.infer<typeof Flag>;
export type StrokeT = z.infer<typeof Stroke>;
export type ClipAnalysisT = z.infer<typeof ClipAnalysis>;

/** JSON schema text embedded in the prompt (mirrors the local pipeline). */
export const CLIP_JSON_SCHEMA = {
  type: "object",
  properties: {
    start_s: { type: "number" },
    end_s: { type: "number" },
    strokes: {
      type: "array",
      items: {
        type: "object",
        properties: {
          t_s: { type: "number", description: "absolute seconds in the FULL video" },
          player: { enum: Player.options },
          stroke: { enum: StrokeType.options },
          technique_flags: {
            type: "array",
            items: {
              type: "object",
              properties: {
                code: { type: "string" },
                severity: { enum: Severity.options },
                evidence: { type: "string" },
              },
              required: ["code", "severity", "evidence"],
              additionalProperties: false,
            },
          },
          tactical_flags: { $ref: "#/properties/strokes/items/properties/technique_flags" },
        },
        required: ["t_s", "player", "stroke", "technique_flags", "tactical_flags"],
        additionalProperties: false,
      },
    },
    rally_summary: { type: "string" },
    confidence: { enum: Confidence.options },
    notes: { type: "string" },
  },
  required: ["start_s", "end_s", "strokes", "rally_summary", "confidence", "notes"],
  additionalProperties: false,
} as const;
