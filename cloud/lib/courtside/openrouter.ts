/**
 * OpenRouter client: chat/completions with image data URLs, the same
 * strict-schema-then-fallback behavior as the local ServerVLM, and the
 * strip-think / extract-JSON tolerance ported from courtside/vlm.py.
 */

const BASE = "https://openrouter.ai/api/v1";
export const DEFAULT_MODEL = "qwen/qwen2.5-vl-72b-instruct";

let schemaOk = true; // flips off after a model rejects response_format

export function stripThink(text: string): string {
  let t = text.replace(/<think>[\s\S]*?<\/think>/g, "");
  if (t.includes("</think>")) t = t.slice(t.lastIndexOf("</think>") + 8);
  if (t.includes("<think>")) t = t.slice(0, t.indexOf("<think>"));
  return t.trim();
}

function firstJsonObject(s: string): unknown | null {
  const start = s.indexOf("{");
  if (start === -1) return null;
  let depth = 0, inStr = false, esc = false;
  for (let i = start; i < s.length; i++) {
    const ch = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === "\\") esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') inStr = true;
    else if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(s.slice(start, i + 1)); } catch { return null; }
      }
    }
  }
  const end = s.lastIndexOf("}");
  if (end > start) {
    try { return JSON.parse(s.slice(start, end + 1)); } catch { return null; }
  }
  return null;
}

export function extractJson(text: string): unknown {
  if (!text?.trim()) throw new Error("model returned empty output");
  for (const cand of [stripThink(text), text]) {
    const fence = cand.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
    const body = fence ? fence[1] : cand;
    const obj = firstJsonObject(body);
    if (obj && typeof obj === "object") return obj;
  }
  throw new Error("no JSON object found in model output");
}

export interface GenOpts {
  images?: string[]; // data URLs
  maxTokens?: number;
  temperature?: number;
  jsonSchema?: unknown;
}

export async function generate(prompt: string, opts: GenOpts = {}): Promise<string> {
  const key = process.env.OPENROUTER_API_KEY;
  if (!key) throw new Error("OPENROUTER_API_KEY is not set");
  const model = process.env.OPENROUTER_MODEL || DEFAULT_MODEL;

  const content: unknown[] = [{ type: "text", text: prompt }];
  for (const url of opts.images ?? []) {
    content.push({ type: "image_url", image_url: { url } });
  }
  const body: Record<string, unknown> = {
    model,
    messages: [{ role: "user", content }],
    max_tokens: opts.maxTokens ?? 1400,
    temperature: opts.temperature ?? 0,
  };
  const useSchema = opts.jsonSchema != null && schemaOk;
  if (useSchema) {
    body.response_format = {
      type: "json_schema",
      json_schema: { name: "ClipAnalysis", strict: true, schema: opts.jsonSchema },
    };
  }

  const call = async (b: Record<string, unknown>) =>
    fetch(`${BASE}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://courtside.app",
        "X-Title": "Courtside",
      },
      body: JSON.stringify(b),
    });

  let res = await call(body);
  if (res.status === 400 && useSchema) {
    // model rejected response_format: retry without and stop sending it
    schemaOk = false;
    delete body.response_format;
    res = await call(body);
  }
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 400);
    throw new Error(`OpenRouter ${res.status}: ${detail}`);
  }
  const data = (await res.json()) as { choices?: { message?: { content?: string } }[] };
  return data.choices?.[0]?.message?.content ?? "";
}
