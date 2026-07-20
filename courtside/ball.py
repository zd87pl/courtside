"""Ball-at-contact height: the strike-zone success factor.

Physics note that makes this honest: at the moment of contact the ball is at
the racquet - i.e. at the PLAYER'S depth plane - so its height relative to the
player's own body landmarks is a valid image-plane measurement (the player is
the scale reference). We therefore measure ball height ONLY at/near contact
and express it in body zones, never in absolute units, and we never measure
balls away from the player (different depth plane = misleading).

Detection is layered:
  1. YOLO "sports ball" detections (COCO class 32; separate model from pose)
  2. motion-blob candidates (frame differencing; catches blurred balls)
  3. candidates are gated by a wrist prior: at contact the ball must be within
     racquet reach of the striking wrist (~1.8 torso lengths)
  4. no confident ball -> fall back to the wrist as a labeled proxy

Output is a zone (below_knee ... above_head) + a stroke-type-aware quality
verdict (ideal / acceptable / poor) + the method used - so the UI can mark
good and bad contact positioning without overclaiming precision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .pose import CONF_MIN, FramePose, L_ANK, L_HIP, L_KNE, L_SHO, L_WRI, R_ANK, R_HIP, R_KNE, R_SHO, R_WRI, torso_len

BALL_CLASS = 32  # COCO "sports ball"
WRIST_REACH_TORSO = 1.8  # racquet length in torso units: contact gate

ZONES = ["below_knee", "knee_to_hip", "hip_to_chest", "chest_to_shoulder",
         "shoulder_to_head", "above_head"]

# stroke-type-aware verdicts: the coaching bands
_GROUND = {"hip_to_chest": "ideal", "knee_to_hip": "acceptable",
           "chest_to_shoulder": "acceptable", "below_knee": "poor",
           "shoulder_to_head": "poor", "above_head": "poor"}
_VOLLEY = {"hip_to_chest": "ideal", "chest_to_shoulder": "ideal",
           "knee_to_hip": "acceptable", "shoulder_to_head": "acceptable",
           "below_knee": "poor", "above_head": "poor"}
_OVERHEAD = {"above_head": "ideal", "shoulder_to_head": "acceptable",
             "chest_to_shoulder": "poor", "hip_to_chest": "poor",
             "knee_to_hip": "poor", "below_knee": "poor"}
QUALITY_BANDS: dict[str, dict[str, str]] = {
    "forehand": _GROUND, "backhand": _GROUND, "return": _GROUND, "slice": _GROUND,
    "drop_shot": _GROUND, "lob": _GROUND, "unknown": _GROUND,
    "forehand_volley": _VOLLEY, "backhand_volley": _VOLLEY,
    "serve": _OVERHEAD, "overhead": _OVERHEAD,
}


def _mean_y(kpts: np.ndarray, i: int, j: int) -> float | None:
    ys = [kpts[k, 1] for k in (i, j) if kpts[k, 2] >= CONF_MIN]
    return float(np.mean(ys)) if ys else None


def body_lines(pose: FramePose) -> dict[str, float] | None:
    """Landmark heights (image y) used as zone boundaries."""
    k = pose.kpts
    hip = _mean_y(k, L_HIP, R_HIP)
    sho = _mean_y(k, L_SHO, R_SHO)
    knee = _mean_y(k, L_KNE, R_KNE)
    if hip is None or sho is None or knee is None or hip <= sho:
        return None
    torso = hip - sho  # px, y grows downward
    return {
        "knee": knee, "hip": hip,
        "chest": hip - 0.6 * torso,
        "shoulder": sho,
        "head": sho - 0.6 * torso,  # crown estimate above shoulder line
    }


def classify_zone(ball_y: float, lines: dict[str, float]) -> str:
    if ball_y > lines["knee"]:
        return "below_knee"
    if ball_y > lines["hip"]:
        return "knee_to_hip"
    if ball_y > lines["chest"]:
        return "hip_to_chest"
    if ball_y > lines["shoulder"]:
        return "chest_to_shoulder"
    if ball_y > lines["head"]:
        return "shoulder_to_head"
    return "above_head"


def quality_for(stroke_type: str, zone: str) -> str:
    return QUALITY_BANDS.get(stroke_type, _GROUND).get(zone, "acceptable")


def _wrists(pose: FramePose) -> list[tuple[float, float]]:
    out = []
    for w in (L_WRI, R_WRI):
        if pose.kpts[w, 2] >= CONF_MIN:
            out.append((float(pose.kpts[w, 0]), float(pose.kpts[w, 1])))
    return out


# ---------------- detectors ----------------

class BallDetector:
    """YOLO sports-ball detector (separate weights from the pose model)."""

    def __init__(self, model_name: str | None = None):
        import os

        from ultralytics import YOLO
        self.model = YOLO(model_name or os.environ.get("COURTSIDE_BALL_MODEL", "yolo11n.pt"))

    def detect(self, frame_path: Path) -> list[tuple[float, float, float]]:
        """(x, y, conf) centers of sports-ball detections."""
        results = self.model.predict(str(frame_path), classes=[BALL_CLASS],
                                     conf=0.15, verbose=False)
        out = []
        for r in results:
            if r.boxes is None:
                continue
            xyxy = r.boxes.xyxy.cpu().numpy() if hasattr(r.boxes.xyxy, "cpu") else np.asarray(r.boxes.xyxy)
            confs = r.boxes.conf.cpu().numpy() if hasattr(r.boxes.conf, "cpu") else np.asarray(r.boxes.conf)
            for b, c in zip(xyxy, confs):
                out.append((float((b[0] + b[2]) / 2), float((b[1] + b[3]) / 2), float(c)))
        return out


def motion_ball_candidates(prev_p: Path, cur_p: Path, next_p: Path,
                           max_area: float = 500.0) -> list[tuple[float, float]]:
    """Small fast-moving blobs via double frame differencing (catches blur)."""
    imgs = [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in (prev_p, cur_p, next_p)]
    if any(i is None for i in imgs) or len({i.shape for i in imgs}) != 1:
        return []
    d1 = cv2.absdiff(imgs[1], imgs[0])
    d2 = cv2.absdiff(imgs[1], imgs[2])
    both = cv2.bitwise_and(d1, d2)
    _, th = cv2.threshold(both, 25, 255, cv2.THRESH_BINARY)
    th = cv2.dilate(th, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    for c in contours:
        area = cv2.contourArea(c)
        if not (3.0 <= area <= max_area):
            continue
        x, y, w, h = cv2.boundingRect(c)
        if max(w, h) / max(1, min(w, h)) > 4.0:  # allow blur streaks, cap extreme
            continue
        cands.append((x + w / 2.0, y + h / 2.0))
    return cands


def find_ball_at_contact(
    pose: FramePose,
    frame_paths: list[Path],
    idx: int,
    detector: "BallDetector | None",
) -> tuple[tuple[float, float], str, str] | None:
    """Ball (x,y) near the striking wrist at the contact frame.

    Returns ((x, y), method, confidence) or None. The wrist-reach gate is what
    keeps the measurement at the player's depth plane.
    """
    t = torso_len(pose.kpts)
    wrists = _wrists(pose)
    if not t or not wrists:
        return None
    reach = WRIST_REACH_TORSO * t

    def nearest_ok(cands: list[tuple[float, float]]) -> tuple[tuple[float, float], float] | None:
        best, best_d = None, 1e9
        for (x, y) in cands:
            d = min(np.hypot(x - wx, y - wy) for wx, wy in wrists)
            if d < best_d:
                best, best_d = (x, y), d
        if best is not None and best_d <= reach:
            return best, best_d
        return None

    if detector is not None:
        yolo = [(x, y) for x, y, _c in detector.detect(frame_paths[idx])]
        hit = nearest_ok(yolo)
        if hit:
            return hit[0], "ball_yolo", "high"
    if 0 < idx < len(frame_paths) - 1:
        blobs = motion_ball_candidates(frame_paths[idx - 1], frame_paths[idx], frame_paths[idx + 1])
        hit = nearest_ok(blobs)
        if hit:
            return hit[0], "ball_motion", "medium"
    return None


def assess_contact(
    pose: FramePose,
    stroke_type: str,
    frame_paths: list[Path],
    idx: int,
    detector: "BallDetector | None",
) -> dict[str, Any] | None:
    """Full contact-height assessment for one stroke at its contact frame."""
    lines = body_lines(pose)
    if lines is None:
        return None
    found = find_ball_at_contact(pose, frame_paths, idx, detector)
    if found:
        (bx, by), method, conf = found
    else:
        wrists = _wrists(pose)
        if not wrists:
            return None
        # labeled proxy: highest wrist approximates racquet-hand contact height
        bx, by = min(wrists, key=lambda w: w[1])
        method, conf = "wrist_proxy", "low"
    zone = classify_zone(by, lines)
    torso = lines["hip"] - lines["shoulder"]
    return {
        "zone": zone,
        "quality": quality_for(stroke_type, zone),
        "method": method,
        "confidence": conf,
        "ball_px": [round(bx, 1), round(by, 1)],
        "height_ratio": round((lines["hip"] - by) / torso, 2),  # 0=hip 1=shoulder
        "lines_px": {k: round(v, 1) for k, v in lines.items()},
    }


def summarize_contacts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Session-level success-factor stats: zone quality distribution by stroke."""
    by_type: dict[str, dict[str, int]] = {}
    total = ideal = 0
    for r in records:
        c = r.get("contact")
        if not c:
            continue
        st = r.get("stroke", "unknown")
        bucket = by_type.setdefault(st, {"ideal": 0, "acceptable": 0, "poor": 0})
        q = c.get("quality", "acceptable")
        bucket[q] = bucket.get(q, 0) + 1
        total += 1
        ideal += 1 if q == "ideal" else 0
    if total == 0:
        return {}
    return {
        "strokes_measured": total,
        "pct_ideal": round(100 * ideal / total),
        "by_type": by_type,
        "note": "ball height at contact, relative to the player's own body zones "
                "(2D image-plane; wrist proxy when the ball is not detected)",
    }
