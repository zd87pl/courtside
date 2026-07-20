"""Biomechanics math, moment selection, and court mapping - no model needed."""

from __future__ import annotations

import numpy as np
import pytest

from courtside.moments import find_reference_stroke, select_flagged_strokes
from courtside.pose import (
    FramePose,
    ankle_midpoint,
    compute_angles,
    normalize_to,
    pick_subject,
    refine_contact_idx,
    torso_len,
    wrist_speeds,
)
from courtside.schema import ClipAnalysis, Flag, Stroke


def _kpts(overrides: dict[int, tuple[float, float]], conf: float = 0.9) -> np.ndarray:
    """A neutral standing skeleton with selective joint placement."""
    base = {
        5: (90, 100), 6: (110, 100),    # shoulders
        7: (80, 130), 8: (120, 130),    # elbows
        9: (75, 160), 10: (125, 160),   # wrists
        11: (92, 160), 12: (108, 160),  # hips
        13: (90, 200), 14: (110, 200),  # knees
        15: (90, 240), 16: (110, 240),  # ankles
    }
    base.update(overrides)
    k = np.zeros((17, 3), dtype=np.float32)
    for i, (x, y) in base.items():
        k[i] = (x, y, conf)
    return k


def test_right_angle_elbow():
    # shoulder above elbow, wrist horizontal from elbow -> 90 degrees
    k = _kpts({6: (100, 100), 8: (100, 130), 10: (130, 130)})
    a = compute_angles(k)
    assert a["elbow_right_deg"] == pytest.approx(90.0, abs=0.5)


def test_straight_leg_is_180():
    k = _kpts({12: (110, 160), 14: (110, 200), 16: (110, 240)})
    a = compute_angles(k)
    assert a["knee_right_deg"] == pytest.approx(180.0, abs=0.5)


def test_occluded_joint_yields_none():
    k = _kpts({})
    k[10, 2] = 0.1  # right wrist below confidence gate
    a = compute_angles(k)
    assert a["elbow_right_deg"] is None
    assert a["elbow_left_deg"] is not None


def test_hip_shoulder_separation_zero_when_parallel():
    a = compute_angles(_kpts({}))
    assert a["hip_shoulder_separation_deg"] == pytest.approx(0.0, abs=0.5)


def test_contact_height_ratio_scale():
    # wrist exactly at shoulder height -> 1.0
    k = _kpts({9: (75, 100), 10: (125, 100)})
    assert compute_angles(k)["contact_height_ratio"] == pytest.approx(1.0, abs=0.05)


def test_torso_and_ankles():
    k = _kpts({})
    assert torso_len(k) == pytest.approx(60.0, abs=1.0)
    fp = FramePose(kpts=k, bbox=(70, 90, 130, 250))
    mx, my = ankle_midpoint(fp)
    assert mx == pytest.approx(100.0) and my == pytest.approx(240.0)


def test_pick_subject_near_vs_far():
    near = FramePose(kpts=_kpts({}), bbox=(0, 200, 200, 600))   # big, low
    far = FramePose(kpts=_kpts({}), bbox=(300, 50, 360, 170))   # small, high
    assert pick_subject([near, far], "near", 600) is near
    assert pick_subject([near, far], "far", 600) is far
    assert pick_subject([], "near", 600) is None


def test_wrist_speed_contact_refinement():
    # wrist jumps far between frames 3 and 4 -> peak at index 4
    frames = []
    for i in range(8):
        wx = 125 + (60 if i >= 4 else 0)
        frames.append(FramePose(kpts=_kpts({10: (wx, 160)}), bbox=(0, 0, 200, 300)))
    speeds = wrist_speeds(frames)
    assert int(np.argmax(speeds)) == 4
    assert refine_contact_idx(frames, nominal_idx=2, max_shift=6) == 4


def test_normalize_to_maps_hip_center_and_scale():
    anchor = _kpts({})
    ref = _kpts({})
    ref[:, :2] = ref[:, :2] * 2 + 500  # bigger, elsewhere
    mapped = normalize_to(anchor, ref)
    assert mapped is not None
    # hip centers must coincide after normalization
    a_hip = (anchor[11, :2] + anchor[12, :2]) / 2
    m_hip = (mapped[11, :2] + mapped[12, :2]) / 2
    assert np.allclose(a_hip, m_hip, atol=1.0)
    assert torso_len(mapped) == pytest.approx(torso_len(anchor), abs=1.0)


# ---------------- moment selection ----------------

def _session():
    return [ClipAnalysis(
        start_s=0, end_s=30, confidence="high", rally_summary="r",
        strokes=[
            Stroke(t_s=5, player="near", stroke="forehand",
                   technique_flags=[Flag(code="late_preparation", severity="high", evidence="e")]),
            Stroke(t_s=9, player="near", stroke="forehand"),  # clean: reference candidate
            Stroke(t_s=12, player="far", stroke="serve",
                   technique_flags=[Flag(code="low_ball_toss", severity="medium", evidence="e")]),
            Stroke(t_s=15, player="near", stroke="backhand",
                   technique_flags=[Flag(code="x", severity="low", evidence="e")]),  # below cutoff
        ])]


def test_select_flagged_strokes_orders_and_filters():
    sel = select_flagged_strokes(_session())
    assert [m["code"] for m in sel] == ["late_preparation", "low_ball_toss"]
    assert sel[0]["severity"] == "high"


def test_select_caps():
    assert len(select_flagged_strokes(_session(), cap=1)) == 1


def test_find_reference_same_type_unflagged():
    assert find_reference_stroke(_session(), "forehand", "near", exclude_t=5.0) == 9.0
    assert find_reference_stroke(_session(), "serve", "far", exclude_t=12.0) is None


# ---------------- court mapping ----------------

def test_court_homography_on_synthetic_frame(tmp_path):
    import cv2
    cv2_img = np.full((360, 640, 3), (40, 120, 45), dtype=np.uint8)  # green
    cv2.rectangle(cv2_img, (100, 60), (540, 320), (255, 255, 255), 6)  # white court boundary
    fp = tmp_path / "court.jpg"
    cv2.imwrite(str(fp), cv2_img)

    from courtside.court import build_courtmap, detect_court_homography, image_to_court
    H = detect_court_homography(fp)
    assert H is not None
    # image center should land mid-court
    x, y = image_to_court(H, (320, 190))
    assert 4 < x < 7 and 10 < y < 14
    doc = build_courtmap(fp, [{"t_s": 1.0, "player": "near", "code": "c",
                               "severity": "high", "xy": (320, 190)}],
                         tmp_path / "courtmap.json")
    assert doc and len(doc["positions"]) == 1


def test_court_detection_fails_gracefully(tmp_path):
    import cv2
    img = np.random.randint(0, 60, (200, 300, 3), dtype=np.uint8)  # dark noise, no court
    fp = tmp_path / "noise.jpg"
    cv2.imwrite(str(fp), img)
    from courtside.court import detect_court_homography
    assert detect_court_homography(fp) is None