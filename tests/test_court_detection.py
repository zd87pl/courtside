"""Robust court detection: line-model fitting, multi-anchor, manual calibration."""

from __future__ import annotations

import json

import cv2
import numpy as np

from courtside.court import (COURT_L, COURT_W, build_serve_return_map,
                             detect_court_homography, homography_from_corners,
                             image_to_court, _MODEL_LINES)
from courtside.webapp import recalibrate_session

# a plausible fixed-camera perspective: doubles corners in a 1000x640 image
_IMG_W, _IMG_H = 1000, 640
_CORNERS_PX = [(300.0, 200.0), (700.0, 200.0), (920.0, 560.0), (80.0, 560.0)]
# court->image mapping used to synthesize test frames
_H_C2I = cv2.getPerspectiveTransform(
    np.array([[0, 0], [COURT_W, 0], [COURT_W, COURT_L], [0, COURT_L]], dtype=np.float32),
    np.array(_CORNERS_PX, dtype=np.float32))


def _proj(x_m: float, y_m: float) -> tuple[float, float]:
    p = cv2.perspectiveTransform(np.array([[[x_m, y_m]]], dtype=np.float64), _H_C2I)[0, 0]
    return float(p[0]), float(p[1])


def _realistic_court_frame(tmp_path, occlude_far_baseline=True, net_band=True):
    """Court LINES under perspective (not a filled quad), far baseline hidden
    behind the net band - the geometry that breaks the largest-white-quad
    detector on real footage."""
    img = np.full((_IMG_H, _IMG_W, 3), (140, 90, 35), dtype=np.uint8)  # blue-ish court
    img[:190, :] = (60, 130, 60)  # green backdrop above the court
    for x0, y0, x1, y1 in _MODEL_LINES:
        p0, p1 = _proj(x0, y0), _proj(x1, y1)
        cv2.line(img, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])), (255, 255, 255), 4)
    if occlude_far_baseline:
        # wipe most of the far baseline (as the net does on low cameras)
        cv2.rectangle(img, (330, 196), (670, 205), (140, 90, 35), -1)
    if net_band:
        # white net band distractor across the middle of the court
        n0, n1 = _proj(0, COURT_L / 2), _proj(COURT_W, COURT_L / 2)
        cv2.line(img, (int(n0[0]), int(n0[1])), (int(n1[0]), int(n1[1])), (250, 250, 250), 9)
    p = tmp_path / "frame.jpg"
    cv2.imwrite(str(p), img)
    return p


def test_line_detector_survives_net_occlusion(tmp_path):
    frame = _realistic_court_frame(tmp_path)
    H = detect_court_homography(frame)
    assert H is not None, "line-model detector failed on realistic perspective court"
    # a known on-court point must map close to its true position
    for x_m, y_m in ((COURT_W / 2, COURT_L - 1.0), (2.0, 3.0), (9.0, 20.0)):
        px = _proj(x_m, y_m)
        got = image_to_court(H, px)
        assert got is not None
        assert abs(got[0] - x_m) < 0.7 and abs(got[1] - y_m) < 0.9, \
            f"({x_m},{y_m}) mapped to {got}"


def test_manual_corner_calibration_maps_correctly():
    H = homography_from_corners(_CORNERS_PX)
    got = image_to_court(H, _proj(COURT_W / 2, COURT_L / 2))
    assert got is not None
    assert abs(got[0] - COURT_W / 2) < 0.05 and abs(got[1] - COURT_L / 2) < 0.05


def _rec(t, stroke, player, x_m, y_m, quality="acceptable"):
    px = _proj(x_m, y_m)
    return {"t_s": t, "player": player, "stroke": stroke,
            "contact": {"zone": "hip_to_chest", "quality": quality,
                        "method": "wrist_proxy", "confidence": "low", "height_ratio": 0.5},
            "ankle_px": [round(px[0], 1), round(px[1], 1)]}


def test_serve_return_map_with_manual_H(tmp_path):
    H = homography_from_corners(_CORNERS_PX)
    recs = [
        _rec(1, "serve", "near", 3.0, 22.5),
        _rec(3, "return", "far", 7.0, 1.5),
        _rec(5, "serve", "near", 8.0, 23.0),
        _rec(7, "return", "far", 3.5, 2.5),
    ]
    doc = build_serve_return_map(None, recs, set(), tmp_path / "sr.json", H=H)
    assert doc["court_detected"] is True
    assert len(doc["positions"]) == 4


def test_multi_anchor_falls_through_to_good_frame(tmp_path):
    """First anchor useless (blank), second is a detectable court."""
    blank = tmp_path / "a0.jpg"
    cv2.imwrite(str(blank), np.full((360, 640, 3), 60, dtype=np.uint8))
    good = _realistic_court_frame(tmp_path)
    recs = [
        _rec(1, "serve", "near", 3.0, 22.5),
        _rec(3, "return", "far", 7.0, 1.5),
        _rec(5, "serve", "near", 8.0, 23.0),
        _rec(7, "return", "far", 3.5, 2.5),
    ]
    doc = build_serve_return_map([blank, good], recs, set(), tmp_path / "sr.json")
    assert doc["court_detected"] is True and len(doc["positions"]) >= 3


def test_recalibrate_session_rebuilds_court_analyses(tmp_path):
    sdir = tmp_path / "m_courtside"
    sdir.mkdir()
    records = [
        _rec(1, "serve", "near", 3.0, 22.5),
        _rec(3, "return", "far", 7.0, 1.5),
        _rec(5, "forehand", "near", 5.5, 20.0, quality="poor"),
        _rec(7, "backhand", "far", 4.0, 3.0),
    ]
    clips = [{"index": 0, "analysis": {"start_s": 0, "end_s": 10, "strokes": [
        {"t_s": 1, "player": "near", "stroke": "serve", "outcome": "net",
         "technique_flags": [{"code": "low_ball_toss", "severity": "medium", "evidence": "e"}],
         "tactical_flags": []},
        {"t_s": 5, "player": "near", "stroke": "forehand", "outcome": "out_long",
         "technique_flags": [], "tactical_flags": []},
    ]}}]
    doc = {"video": "m.mov", "facts": {"total_strokes": 4},
           "contact_quality": {"summary": {"strokes_measured": 4}, "strokes": records},
           "clips": clips}
    (sdir / "session.json").write_text(json.dumps(doc))

    result = recalibrate_session(sdir, [list(c) for c in _CORNERS_PX])
    assert result["ok"] and result["court_detected"]
    saved = json.loads((sdir / "session.json").read_text())
    assert saved["court_calibration"] == "manual"
    assert saved["serve_return"]["court_detected"] is True
    assert saved["error_matrix"]["court_detected"] is True
    assert saved["error_matrix"]["players"]["near"]["errors"] >= 2
    assert (sdir / "court_manual.json").exists()
