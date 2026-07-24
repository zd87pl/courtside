/** Pure-logic parity tests (run: node --experimental-strip-types --test test/). */
import assert from "node:assert";
import { test } from "node:test";
import { detectSegments, fixedWindows } from "../lib/client/video.ts";
import { computeSessionFacts, normalizeFlagCode } from "../lib/courtside/facts.ts";
import { extractJson, stripThink } from "../lib/courtside/openrouter.ts";
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
