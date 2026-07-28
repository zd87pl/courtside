"""Portion parsing, GDrive URL handling, position zones, serve/return map."""

from __future__ import annotations

import numpy as np
import pytest

from courtside.analyze import parse_ts
from courtside.court import COURT_L, COURT_W, build_serve_return_map, position_zone
from courtside.fetch import gdrive_id


def test_parse_ts_forms():
    assert parse_ts("90") == 90
    assert parse_ts("1:30") == 90
    assert parse_ts("1:02:03") == 3723
    with pytest.raises(ValueError):
        parse_ts("1:2:3:4")
    with pytest.raises(ValueError):
        parse_ts("::")


def test_gdrive_id_shapes():
    assert gdrive_id("https://drive.google.com/file/d/1AbC_dEf-234567890/view?usp=sharing") == "1AbC_dEf-234567890"
    assert gdrive_id("https://drive.google.com/open?id=1AbC_dEf-234567890") == "1AbC_dEf-234567890"
    assert gdrive_id("https://drive.google.com/uc?export=download&id=1AbC_dEf-234567890") == "1AbC_dEf-234567890"
    assert gdrive_id("https://youtube.com/watch?v=x") is None


def test_position_zones_near_and_far():
    # near player standing 1m behind their baseline, on their left third
    z = position_zone(1.5, COURT_L + 1.0)
    assert z == {"half": "near", "depth": "behind_baseline", "lateral": "left", "zone": "behind_baseline_left"}
    # far player same spot on THEIR side: mirrored, still 'left' for them
    z2 = position_zone(COURT_W - 1.5, -1.0)
    assert z2["half"] == "far" and z2["zone"] == "behind_baseline_left"
    # net zone, center
    z3 = position_zone(COURT_W / 2, COURT_L / 2 + 1.5)
    assert z3["depth"] == "net" and z3["lateral"] == "center"
    # midcourt
    z4 = position_zone(COURT_W / 2, COURT_L / 2 + 4.0)
    assert z4["depth"] == "midcourt"


def _rec(t, stroke, player, zone="hip_to_chest", quality="ideal", ankle=None):
    return {"t_s": t, "player": player, "stroke": stroke,
            "contact": {"zone": zone, "quality": quality, "method": "wrist_proxy",
                        "confidence": "low", "height_ratio": 0.5},
            **({"ankle_px": ankle} if ankle else {})}


def test_serve_return_map_heights_without_court(tmp_path):
    records = [
        _rec(1, "serve", "near"),
        _rec(3, "return", "far", zone="below_knee", quality="poor"),
        _rec(9, "return", "far", zone="hip_to_chest", quality="ideal"),
        _rec(5, "forehand", "near"),  # not serve/return: excluded
    ]
    doc = build_serve_return_map(None, records, set(), tmp_path / "sr.json")
    assert doc["counts"] == {"serves": 1, "returns": 2}
    assert doc["return_height"]["zones"] == {"below_knee": 1, "hip_to_chest": 1}
    assert doc["return_height"]["quality"]["poor"] == 1
    assert doc["court_detected"] is False and doc["positions"] == []
    assert (tmp_path / "sr.json").exists()


def test_serve_return_map_with_court(tmp_path):
    import cv2
    img = np.full((360, 640, 3), (40, 120, 45), dtype=np.uint8)
    cv2.rectangle(img, (100, 60), (540, 320), (255, 255, 255), 6)
    anchor = tmp_path / "anchor.jpg"
    cv2.imwrite(str(anchor), img)
    records = [
        _rec(1, "serve", "near", ankle=[320, 300]),   # bottom center: near baseline area
        _rec(3, "return", "far", ankle=[320, 80], zone="below_knee", quality="poor"),
    ]
    doc = build_serve_return_map(anchor, records, {3.0}, tmp_path / "sr.json")
    assert doc["court_detected"] is True
    assert len(doc["positions"]) == 2
    near = next(p for p in doc["positions"] if p["stroke"] == "serve")
    far = next(p for p in doc["positions"] if p["stroke"] == "return")
    assert near["half"] == "near" and far["half"] == "far"
    assert far["flagged"] is True and far["quality"] == "poor"
    assert any(k.startswith("serve:") for k in doc["zones_summary"])
