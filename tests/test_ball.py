"""Strike-zone logic: zone classification, verdicts, detection fusion, fallbacks."""

from __future__ import annotations

import numpy as np

from courtside.ball import (
    assess_contact,
    body_lines,
    classify_zone,
    find_ball_at_contact,
    motion_ball_candidates,
    quality_for,
    summarize_contacts,
)
from courtside.pose import FramePose


def _kpts() -> np.ndarray:
    """Standing skeleton: shoulders y=100, hips y=160, knees y=200, wrists y=160."""
    base = {5: (90, 100), 6: (110, 100), 7: (80, 130), 8: (120, 130),
            9: (75, 160), 10: (125, 160), 11: (92, 160), 12: (108, 160),
            13: (90, 200), 14: (110, 200), 15: (90, 240), 16: (110, 240)}
    k = np.zeros((17, 3), dtype=np.float32)
    for i, (x, y) in base.items():
        k[i] = (x, y, 0.9)
    return k


def _pose() -> FramePose:
    return FramePose(kpts=_kpts(), bbox=(70, 90, 130, 250))


def test_body_lines_and_zones():
    lines = body_lines(_pose())
    assert lines["hip"] == 160 and lines["shoulder"] == 100 and lines["knee"] == 200
    assert lines["chest"] == 124  # hip - 0.6 * torso(60)
    assert classify_zone(220, lines) == "below_knee"
    assert classify_zone(180, lines) == "knee_to_hip"
    assert classify_zone(140, lines) == "hip_to_chest"
    assert classify_zone(110, lines) == "chest_to_shoulder"
    assert classify_zone(80, lines) == "shoulder_to_head"
    assert classify_zone(30, lines) == "above_head"


def test_quality_bands_by_stroke():
    assert quality_for("forehand", "hip_to_chest") == "ideal"
    assert quality_for("forehand", "below_knee") == "poor"
    assert quality_for("serve", "above_head") == "ideal"
    assert quality_for("serve", "hip_to_chest") == "poor"
    assert quality_for("forehand_volley", "chest_to_shoulder") == "ideal"


def test_motion_candidates_synthetic_ball(tmp_path):
    import cv2
    for i, x in enumerate((40, 60, 80)):  # small bright blob moving right
        img = np.zeros((120, 160), dtype=np.uint8)
        cv2.circle(img, (x, 50), 3, 255, -1)
        cv2.imwrite(str(tmp_path / f"f{i}.jpg"), img)
    cands = motion_ball_candidates(tmp_path / "f0.jpg", tmp_path / "f1.jpg", tmp_path / "f2.jpg")
    assert any(abs(cx - 60) < 8 and abs(cy - 50) < 8 for cx, cy in cands)


class _FakeDetector:
    def __init__(self, balls):
        self.balls = balls

    def detect(self, frame_path):
        return self.balls


def test_find_ball_uses_wrist_gate():
    pose = _pose()
    frames = ["f0", "f1", "f2"]
    # ball near the right wrist (125,160): within reach
    (bx, by), method, conf = find_ball_at_contact(
        pose, frames, 1, _FakeDetector([(150.0, 140.0, 0.8)]))
    assert (bx, by) == (150.0, 140.0) and method == "ball_yolo" and conf == "high"
    # ball far across the frame: rejected (different depth plane)
    assert find_ball_at_contact(pose, frames, 1, _FakeDetector([(600.0, 40.0, 0.9)])) is None


def test_assess_contact_ball_and_proxy(tmp_path):
    pose = _pose()
    frames = [tmp_path / f"f{i}.jpg" for i in range(3)]
    # detected ball at chest height near wrist -> ideal for a forehand
    c = assess_contact(pose, "forehand", frames, 1, _FakeDetector([(140.0, 140.0, 0.9)]))
    assert c["zone"] == "hip_to_chest" and c["quality"] == "ideal"
    assert c["method"] == "ball_yolo" and c["confidence"] == "high"
    assert 0 <= c["height_ratio"] <= 1
    # no detector, no motion frames on disk -> labeled wrist proxy
    c2 = assess_contact(pose, "forehand", frames, 1, None)
    assert c2["method"] == "wrist_proxy" and c2["confidence"] == "low"
    assert c2["zone"] == "hip_to_chest"  # wrists sit exactly on the hip line; boundaries classify upward


def test_summarize_contacts():
    recs = [
        {"stroke": "forehand", "contact": {"quality": "ideal"}},
        {"stroke": "forehand", "contact": {"quality": "poor"}},
        {"stroke": "serve", "contact": {"quality": "ideal"}},
        {"stroke": "backhand", "contact": None},  # unmeasured: excluded
    ]
    s = summarize_contacts(recs)
    assert s["strokes_measured"] == 3
    assert s["pct_ideal"] == 67
    assert s["by_type"]["forehand"] == {"ideal": 1, "acceptable": 0, "poor": 1}
    assert summarize_contacts([]) == {}
