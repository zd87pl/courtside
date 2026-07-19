"""Cheap, model-free rally segmentation via motion energy.

This is the demo stand-in for the AceLens specialist-CV stage: it structures
the video BEFORE the VLM sees it, so the expensive model only looks at active
play. Frames are sampled at ~5 fps, downscaled, converted to grayscale, and
scored by mean absolute inter-frame difference. Hysteresis thresholds +
gap-merging turn the activity curve into (start_s, end_s) segments.

Good enough for fixed-camera court footage; swap in the real event pipeline
(F3ED etc.) when porting back to the SaaS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Segment:
    start_s: float
    end_s: float

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s


def _activity_curve(video_path: Path, sample_fps: float = 5.0, width: int = 320):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    stride = max(1, round(src_fps / sample_fps))

    times: list[float] = []
    scores: list[float] = []
    prev = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % stride == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            h = int(frame.shape[0] * width / frame.shape[1])
            small = cv2.resize(frame, (width, h), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if prev is not None:
                scores.append(float(np.mean(np.abs(gray - prev))))
                times.append(idx / src_fps)
            prev = gray
        idx += 1
    cap.release()

    duration = (n_frames / src_fps) if n_frames else (times[-1] if times else 0.0)
    return np.asarray(times), np.asarray(scores), duration


def detect_segments(
    video_path: Path,
    min_rally_s: float = 2.5,
    max_clip_s: float = 45.0,
    merge_gap_s: float = 1.5,
    pad_s: float = 0.5,
    enter_frac: float = 0.35,
    exit_frac: float = 0.15,
) -> list[Segment]:
    """Hysteresis thresholding of the motion-energy curve."""
    times, scores, duration = _activity_curve(video_path)
    if len(scores) == 0:
        return []

    ref = float(np.percentile(scores, 95)) or 1.0
    enter_t, exit_t = enter_frac * ref, exit_frac * ref

    raw: list[Segment] = []
    active = False
    start = 0.0
    for t, s in zip(times, scores):
        if not active and s >= enter_t:
            active, start = True, t
        elif active and s < exit_t:
            active = False
            raw.append(Segment(start, t))
    if active:
        raw.append(Segment(start, float(times[-1])))

    # merge close segments
    merged: list[Segment] = []
    for seg in raw:
        if merged and seg.start_s - merged[-1].end_s <= merge_gap_s:
            merged[-1] = Segment(merged[-1].start_s, seg.end_s)
        else:
            merged.append(seg)

    # pad, clamp, drop short, split long
    final: list[Segment] = []
    for seg in merged:
        s = max(0.0, seg.start_s - pad_s)
        e = min(duration, seg.end_s + pad_s)
        if e - s < min_rally_s:
            continue
        while e - s > max_clip_s:
            final.append(Segment(s, s + max_clip_s))
            s += max_clip_s
        final.append(Segment(s, e))
    return final


def fixed_windows(video_path: Path, window_s: float) -> list[Segment]:
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = n / fps if n else 0.0
    out, t = [], 0.0
    while t < duration:
        out.append(Segment(t, min(t + window_s, duration)))
        t += window_s
    return out


def from_file(path: Path) -> list[Segment]:
    """Load segments from JSON: [[start_s, end_s], ...] or [{"start_s":..,"end_s":..}, ...]."""
    data = json.loads(Path(path).read_text())
    segs = []
    for item in data:
        if isinstance(item, dict):
            segs.append(Segment(float(item["start_s"]), float(item["end_s"])))
        else:
            segs.append(Segment(float(item[0]), float(item[1])))
    return segs
