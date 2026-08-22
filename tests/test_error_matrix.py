"""Outcome/received schema fields, runway lanes, and the depth x lane error matrix."""

from __future__ import annotations

import numpy as np

from courtside.court import (ALLEY_M, COURT_L, COURT_W, SINGLES_W,
                             build_error_matrix, lane_for_x, position_zone)
from courtside.report import compute_session_facts
from courtside.schema import ClipAnalysis, Stroke


def test_stroke_outcome_defaults_and_roundtrip():
    # docs written before the fields existed still validate
    s = Stroke.model_validate({"t_s": 1.0, "player": "near", "stroke": "forehand"})
    assert s.outcome == "unknown" and s.received == "unknown" and s.outcome_confidence == "low"
    s2 = Stroke.model_validate({"t_s": 2.0, "player": "far", "stroke": "backhand",
                                "outcome": "net", "outcome_confidence": "high",
                                "received": "difficult"})
    assert s2.outcome == "net" and s2.received == "difficult"
    assert Stroke.model_validate(s2.model_dump()).outcome == "net"


def _clip(strokes):
    return ClipAnalysis.model_validate({"start_s": 0, "end_s": 30, "strokes": strokes,
                                        "rally_summary": "x", "confidence": "medium"})


def test_outcome_facts_math():
    a = _clip([
        {"t_s": 1, "player": "near", "stroke": "forehand", "outcome": "net",
         "outcome_confidence": "high", "received": "easy"},
        {"t_s": 2, "player": "near", "stroke": "backhand", "outcome": "in_play",
         "outcome_confidence": "medium", "received": "difficult"},
        {"t_s": 3, "player": "near", "stroke": "forehand", "outcome": "out_long",
         "outcome_confidence": "medium", "received": "difficult"},
        {"t_s": 4, "player": "far", "stroke": "serve"},  # unknown outcome
    ])
    oc = compute_session_facts([a])["outcomes"]
    assert oc["decided"] == 3 and oc["errors"] == 2
    assert oc["error_rate_pct"] == round(100 * 2 / 3)
    assert oc["coverage_pct"] == 75
    # only the net-on-easy ball counts as an unforced proxy
    assert oc["unforced_proxy"] == 1


def test_outcome_facts_all_unknown():
    a = _clip([{"t_s": 1, "player": "near", "stroke": "forehand"}])
    oc = compute_session_facts([a])["outcomes"]
    assert oc["decided"] == 0 and oc["error_rate_pct"] is None
    assert compute_session_facts([])["outcomes"]["coverage_pct"] == 0


def test_zone_number_and_runway_template():
    """The coach-facing template: zones 1 (net) .. 5 (back), runways C-L..C-R."""
    mid = COURT_L / 2
    # near player at increasing distance from the net
    assert position_zone(COURT_W / 2, mid + 1.5)["zone_number"] == 1
    assert position_zone(COURT_W / 2, mid + 4.0)["zone_number"] == 2
    assert position_zone(COURT_W / 2, mid + 7.0)["zone_number"] == 3
    assert position_zone(COURT_W / 2, mid + 10.0)["zone_number"] == 4
    assert position_zone(COURT_W / 2, COURT_L + 1.0)["zone_number"] == 5
    # runway labels, mirrored for the far player
    assert position_zone(COURT_W / 2, mid + 10.0)["runway"] == "A"
    assert position_zone(0.5, mid + 10.0)["runway"] == "C-L"
    assert position_zone(COURT_W - 0.5, -1.0)["runway"] == "C-L"  # far player's left


def test_error_matrix_cells_use_template_keys(tmp_path):
    anchor = _court_anchor(tmp_path)
    records = [_rec(1, "forehand", "near", quality="poor", ankle=[320, 300]),
               _rec(3, "backhand", "near", ankle=[250, 295]),
               _rec(5, "forehand", "near", ankle=[420, 305]),
               _rec(7, "backhand", "near", ankle=[350, 280])]
    info = [{"t_s": t, "outcome": "net" if t == 3 else "in_play", "max_severity": ""}
            for t in (1, 3, 5, 7)]
    doc = build_error_matrix(anchor, records, info, tmp_path / "em.json")
    assert doc["court_detected"] is True
    import re
    for cell in doc["players"]["near"]["cells"]:
        assert re.fullmatch(r"z[1-5]:(C-L|B-L|A|B-R|C-R)", cell), cell
    assert doc["runways"] == ["C-L", "B-L", "A", "B-R", "C-R"]


def test_lane_boundaries():
    third = SINGLES_W / 3
    assert lane_for_x(0.5) == "wide_left"
    assert lane_for_x(ALLEY_M) == "left"                    # boundary goes inward
    assert lane_for_x(ALLEY_M + third - 0.01) == "left"
    assert lane_for_x(COURT_W / 2) == "center"              # center service line
    assert lane_for_x(ALLEY_M + 2 * third + 0.01) == "right"
    assert lane_for_x(COURT_W - ALLEY_M) == "wide_right"
    assert lane_for_x(COURT_W + 2.0) == "wide_right"        # out-of-court margin
    assert lane_for_x(-1.0) == "wide_left"


def test_lane_mirroring_for_far_player():
    # far player near THEIR left alley: mirrored x = COURT_W - 0.5 -> wide_left for them
    z = position_zone(COURT_W - 0.5, -1.0)
    assert z["half"] == "far" and z["lane"] == "wide_left"
    z2 = position_zone(0.5, COURT_L + 1.0)
    assert z2["half"] == "near" and z2["lane"] == "wide_left"


def _rec(t, stroke, player, quality="acceptable", ankle=None):
    return {"t_s": t, "player": player, "stroke": stroke,
            "contact": {"zone": "hip_to_chest", "quality": quality,
                        "method": "wrist_proxy", "confidence": "low", "height_ratio": 0.5},
            **({"ankle_px": ankle} if ankle else {})}


def _court_anchor(tmp_path):
    import cv2
    img = np.full((360, 640, 3), (40, 120, 45), dtype=np.uint8)
    cv2.rectangle(img, (100, 60), (540, 320), (255, 255, 255), 6)
    anchor = tmp_path / "anchor.jpg"
    cv2.imwrite(str(anchor), img)
    return anchor


def test_error_matrix_aggregation(tmp_path):
    anchor = _court_anchor(tmp_path)
    records = [
        _rec(1, "forehand", "near", ankle=[320, 300]),                  # outcome error
        _rec(3, "backhand", "near", ankle=[322, 300]),                  # flagged
        _rec(5, "forehand", "near", quality="poor", ankle=[318, 300]),  # poor contact
        _rec(7, "forehand", "near", ankle=[320, 298]),                  # clean
        _rec(9, "return", "far", ankle=[320, 80]),                      # far, outcome error
    ]
    stroke_info = [
        {"t_s": 1, "outcome": "net", "received": "easy", "max_severity": ""},
        {"t_s": 3, "outcome": "in_play", "received": "normal", "max_severity": "medium"},
        {"t_s": 5, "outcome": "unknown", "received": "unknown", "max_severity": "low"},
        {"t_s": 7, "outcome": "in_play", "received": "easy", "max_severity": ""},
        {"t_s": 9, "outcome": "out_long", "received": "difficult", "max_severity": ""},
    ]
    doc = build_error_matrix(anchor, records, stroke_info, tmp_path / "em.json")
    assert doc["court_detected"] is True
    near = doc["players"]["near"]
    assert near["measured"] == 4 and near["errors"] == 3
    cells = near["cells"]
    total = {k: 0 for k in ("net", "flagged", "poor_contact")}
    for c in cells.values():
        for k in total:
            total[k] += c[k]
    assert total == {"net": 1, "flagged": 1, "poor_contact": 1}
    far = doc["players"]["far"]
    assert far["errors"] == 1 and any(c["out_long"] == 1 for c in far["cells"].values())
    # positions only for errors, each with causes
    assert len(doc["positions"]) == 4
    assert all(p["causes"] for p in doc["positions"])
    assert doc["worst_cells"] and doc["worst_cells"][0]["errors"] >= doc["worst_cells"][-1]["errors"]
    assert (tmp_path / "em.json").exists()


def test_error_matrix_without_court(tmp_path):
    doc = build_error_matrix(None, [_rec(1, "forehand", "near", ankle=[10, 10])],
                             [{"t_s": 1, "outcome": "net", "max_severity": ""}],
                             tmp_path / "em.json")
    assert doc["court_detected"] is False and doc["positions"] == []


def test_error_matrix_legacy_stroke_info(tmp_path):
    # stroke_info without outcome keys (old analyses): flags/poor contact still count
    anchor = _court_anchor(tmp_path)
    records = [_rec(1, "forehand", "near", quality="poor", ankle=[320, 300]),
               _rec(3, "backhand", "near", ankle=[322, 300])]
    doc = build_error_matrix(anchor, records,
                             [{"t_s": 1, "max_severity": ""}, {"t_s": 3, "max_severity": "high"}],
                             tmp_path / "em.json")
    near = doc["players"]["near"]
    assert near["errors"] == 2
    assert sum(c["poor_contact"] for c in near["cells"].values()) == 1
    assert sum(c["flagged"] for c in near["cells"].values()) == 1
