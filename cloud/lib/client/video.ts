/**
 * Browser-side video processing: the cloud twin of segment.py + frames.py.
 * The video NEVER uploads - the browser decodes it, finds the rallies via the
 * same motion-energy hysteresis as the local demo, and samples JPEG frames;
 * only those frames go to the analysis API.
 */

export interface Segment {
  startS: number;
  endS: number;
}

export interface ExtractedClip {
  segment: Segment;
  timestamps: number[];
  frames: string[]; // JPEG data URLs
}

function makeCanvas(w: number, h: number): { canvas: HTMLCanvasElement; ctx: CanvasRenderingContext2D } {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("canvas 2d context unavailable");
  return { canvas, ctx };
}

export function loadVideo(file: File): Promise<HTMLVideoElement> {
  return new Promise((resolve, reject) => {
    const v = document.createElement("video");
    v.preload = "auto";
    v.muted = true;
    v.playsInline = true;
    v.src = URL.createObjectURL(file);
    v.onloadedmetadata = () => resolve(v);
    v.onerror = () => reject(new Error("could not decode this video in the browser"));
  });
}

function seek(v: HTMLVideoElement, t: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const onSeek = () => { cleanup(); resolve(); };
    const onErr = () => { cleanup(); reject(new Error("seek failed")); };
    const cleanup = () => {
      v.removeEventListener("seeked", onSeek);
      v.removeEventListener("error", onErr);
    };
    v.addEventListener("seeked", onSeek);
    v.addEventListener("error", onErr);
    v.currentTime = Math.min(Math.max(t, 0), Math.max(0, v.duration - 0.05));
  });
}

/** Mean-abs-diff motion curve at ~2fps on 64px luma - segment.py's activity curve. */
export async function activityCurve(
  v: HTMLVideoElement,
  onProgress?: (frac: number) => void,
): Promise<{ times: number[]; scores: number[] }> {
  const step = 0.5;
  const w = 64;
  const h = Math.max(8, Math.round((v.videoHeight / v.videoWidth) * w) || 36);
  const { canvas, ctx } = makeCanvas(w, h);
  const times: number[] = [];
  const scores: number[] = [];
  let prev: Float32Array | null = null;
  for (let t = 0; t < v.duration; t += step) {
    await seek(v, t);
    ctx.drawImage(v, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h).data;
    const luma = new Float32Array(w * h);
    for (let i = 0; i < luma.length; i++) {
      const j = i * 4;
      luma[i] = 0.299 * d[j] + 0.587 * d[j + 1] + 0.114 * d[j + 2];
    }
    if (prev) {
      let sum = 0;
      for (let i = 0; i < luma.length; i++) sum += Math.abs(luma[i] - prev[i]);
      times.push(t);
      scores.push(sum / luma.length);
    }
    prev = luma;
    onProgress?.(t / v.duration);
  }
  canvas.remove();
  return { times, scores };
}

function percentile(xs: number[], p: number): number {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))] ?? 0;
}

/** Hysteresis + merge + pad + split: the detect_segments port. */
export function detectSegments(
  times: number[],
  scores: number[],
  duration: number,
  opts = { minRallyS: 2.5, maxClipS: 30, mergeGapS: 1.5, padS: 0.5, enterFrac: 0.35, exitFrac: 0.15 },
): Segment[] {
  if (!scores.length) return [];
  const ref = percentile(scores, 95) || 1;
  const enter = opts.enterFrac * ref;
  const exit = opts.exitFrac * ref;

  const raw: Segment[] = [];
  let active = false;
  let start = 0;
  for (let i = 0; i < times.length; i++) {
    if (!active && scores[i] >= enter) { active = true; start = times[i]; }
    else if (active && scores[i] < exit) { active = false; raw.push({ startS: start, endS: times[i] }); }
  }
  if (active) raw.push({ startS: start, endS: times[times.length - 1] });

  const merged: Segment[] = [];
  for (const seg of raw) {
    const last = merged[merged.length - 1];
    if (last && seg.startS - last.endS <= opts.mergeGapS) last.endS = seg.endS;
    else merged.push({ ...seg });
  }
  // constant-motion misfire guard (handheld footage): fall back to fixed windows
  const covered = merged.reduce((a, s) => a + (s.endS - s.startS), 0);
  if (duration > 0 && merged.length <= 1 && covered >= 0.9 * duration) return [];

  const final: Segment[] = [];
  for (const seg of merged) {
    let s = Math.max(0, seg.startS - opts.padS);
    const e = Math.min(duration, seg.endS + opts.padS);
    if (e - s < opts.minRallyS) continue;
    while (e - s > opts.maxClipS) {
      final.push({ startS: s, endS: s + opts.maxClipS });
      s += opts.maxClipS;
    }
    if (e - s >= opts.minRallyS) final.push({ startS: s, endS: e });
  }
  return final;
}

export function fixedWindows(duration: number, windowS = 20): Segment[] {
  const out: Segment[] = [];
  for (let t = 0; t < duration; t += windowS) {
    out.push({ startS: t, endS: Math.min(t + windowS, duration) });
  }
  return out;
}

/** Sample up to maxFrames JPEG frames from one segment. */
export async function extractClip(
  v: HTMLVideoElement,
  segment: Segment,
  maxFrames = 8,
  maxSide = 640,
  quality = 0.7,
): Promise<ExtractedClip> {
  const dur = Math.max(0.1, segment.endS - segment.startS);
  const n = Math.min(maxFrames, Math.max(2, Math.round(dur * 2)));
  const scale = maxSide / Math.max(v.videoWidth, v.videoHeight);
  const w = Math.round(v.videoWidth * Math.min(1, scale));
  const h = Math.round(v.videoHeight * Math.min(1, scale));
  const { canvas, ctx } = makeCanvas(w, h);
  const frames: string[] = [];
  const timestamps: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = segment.startS + (i / Math.max(1, n - 1)) * dur;
    await seek(v, t);
    ctx.drawImage(v, 0, 0, w, h);
    frames.push(canvas.toDataURL("image/jpeg", quality));
    timestamps.push(t);
  }
  canvas.remove();
  return { segment, timestamps, frames };
}
