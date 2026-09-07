/** Pure-logic parity tests (run: node --experimental-strip-types --test test/). */
import assert from "node:assert";
import { test } from "node:test";
import { detectSegments, fixedWindows } from "../lib/client/video.ts";
import { computeSessionFacts, normalizeFlagCode } from "../lib/courtside/facts.ts";
import { DEFAULT_MODEL, extractJson, generate, stripThink } from "../lib/courtside/openrouter.ts";
import { ClipAnalysis } from "../lib/courtside/schema.ts";
import { mdToHtml } from "../lib/md.ts";

function curve(fn: (t: number) => number, dur = 100, dt = 0.5) {
  const times: number[] = [];
  const scores: number[] = [];
  for (let t = dt; t < dur; t += dt) {
    times.push(t);
    scores.push(fn(t));
  }
  return { times, scores };
}

test("segmentation: burst detected, sub-min tail dropped after split", () => {
  const { times, scores } = curve((t) => (t > 10 && t < 42 ? 10 : 0.1));
  const segs = detectSegments(times, scores, 100);
  assert.ok(segs.length >= 1);
  for (const s of segs) assert.ok(s.endS - s.startS >= 2.5, "sub-minimum segment survived");
});

test("segmentation: constant motion rejected (handheld guard)", () => {
  const { times, scores } = curve(() => 5);
  assert.deepEqual(detectSegments(times, scores, 100), []);
});

test("segmentation: two rallies -> two segments", () => {
  const { times, scores } = curve((t) => ((t > 10 && t < 20) || (t > 40 && t < 48) ? 10 : 0.1));
  assert.equal(detectSegments(times, scores, 100).length, 2);
});

test("fixed windows cover the duration", () => {
  const w = fixedWindows(45, 20);
  assert.equal(w.length, 3);
  assert.equal(w[2].endS, 45);
});

test("extractJson: fences, think blocks, trailing prose, empties", () => {
  assert.deepEqual(extractJson('```json\n{"x": 2}\n```'), { x: 2 });
  assert.deepEqual(extractJson('<think>noise {"fake":1}</think>{"y":3}'), { y: 3 });
  assert.deepEqual(extractJson('<think>answer inside {"y":3}'), { y: 3 });
  assert.deepEqual(extractJson('{"a":{"b":"has } brace"}} and prose } ]'), { a: { b: "has } brace" } });
  assert.throws(() => extractJson("   "));
  assert.throws(() => extractJson("no json here"));
  assert.equal(stripThink("a</think>B"), "B");
});

test("OpenRouter: Qwen3.8 frame analysis keeps non-thinking mode through schema fallback", async (t) => {
  const oldKey = process.env.OPENROUTER_API_KEY;
  const oldModel = process.env.OPENROUTER_MODEL;
  t.after(() => {
    if (oldKey === undefined) delete process.env.OPENROUTER_API_KEY;
    else process.env.OPENROUTER_API_KEY = oldKey;
    if (oldModel === undefined) delete process.env.OPENROUTER_MODEL;
    else process.env.OPENROUTER_MODEL = oldModel;
  });
  process.env.OPENROUTER_API_KEY = "test-only";
  delete process.env.OPENROUTER_MODEL;
  const bodies: Record<string, unknown>[] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "https://openrouter.ai/api/v1/chat/completions");
    const body = JSON.parse(init.body as string);
    bodies.push(body);
    if (body.response_format) return new Response("schema rejected", { status: 400 });
    return Response.json({ choices: [{ message: { content: '{"ok":true}' } }] });
  });
  const image = "data:image/jpeg;base64,aW1hZ2U=";
  assert.equal(await generate("analyze frame", { images: [image], jsonSchema: { type: "object" } }), '{"ok":true}');
  assert.equal(DEFAULT_MODEL, "qwen/qwen3.8-27b");
  assert.equal(bodies.length, 2);
  for (const body of bodies) {
    assert.equal(body.model, DEFAULT_MODEL);
    assert.equal(body.max_tokens, 1400);
    assert.deepEqual(body.reasoning, { effort: "none" });
    assert.deepEqual(body.messages, [{ role: "user", content: [
      { type: "text", text: "analyze frame" }, { type: "image_url", image_url: { url: image } },
    ] }]);
  }
  process.env.OPENROUTER_MODEL = "vendor/custom-vision-model";
  await generate("override");
  assert.equal(bodies[2].model, "vendor/custom-vision-model");
  assert.equal(bodies[2].reasoning, undefined);

  delete process.env.OPENROUTER_MODEL;
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    assert.deepEqual(JSON.parse(init.body as string).reasoning, { effort: "none" });
    return new Response("reasoning rejected", { status: 400 });
  });
  await assert.rejects(generate("p"), /OpenRouter 400: reasoning rejected/);
});

test("facts: totals, split, synonym normalization", () => {
  const clip = ClipAnalysis.parse({
    start_s: 0, end_s: 10, rally_summary: "r", confidence: "high", notes: "",
    strokes: [
      { t_s: 1, player: "near", stroke: "forehand",
        technique_flags: [{ code: "late_prep", severity: "high", evidence: "e" }], tactical_flags: [] },
      { t_s: 2, player: "far", stroke: "forehand",
        technique_flags: [{ code: "Late Preparation", severity: "medium", evidence: "e" }], tactical_flags: [] },
    ],
  });
  const f = computeSessionFacts([clip]);
  assert.equal(f.total_strokes, 2);
  assert.deepEqual(f.player_split, { near: 1, far: 1, unknown: 0 });
  assert.equal(f.top_technique_flags["late_preparation"], 2);
  assert.equal(normalizeFlagCode("no_splitstep"), "no_split_step");
});

test("schema: defaults + rejection", () => {
  const ok = ClipAnalysis.parse({ start_s: 0, end_s: 5, rally_summary: "r", confidence: "low" });
  assert.deepEqual(ok.strokes, []);
  assert.throws(() =>
    ClipAnalysis.parse({ start_s: 0, end_s: 5, rally_summary: "r", confidence: "certain" }),
  );
});

test("markdown: paragraphs join, ordered lists keep numbering", () => {
  const html = mdToHtml("## H\nfirst\nsecond\n\n1. one\n   wraps\n2. two\n");
  assert.ok(html.includes("<p>first second</p>"));
  assert.ok(html.includes("<li>one wraps</li>"));
  assert.ok(html.includes("<li>two</li>"));
  assert.ok(!html.includes("<script"));
});
