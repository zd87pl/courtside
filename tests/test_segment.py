"""Segmentation logic - the math that structures the video before the VLM.

These run with no Mac, no model, and no video: the activity curve is injected.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from courtside import segment
from courtside.segment import ActivityCurve, Segment, detect_segments, fixed_windows, from_file


def _curve(score_fn, dur=100.0, dt=0.2):
    t = np.arange(0, dur, dt)
    return ActivityCurve(times=list(t), scores=list(score_fn(t)), duration=dur)


def test_split_drops_sub_minimum_tail():
    # ~46s active burst -> a 45s clip plus a ~2.4s remainder that must be dropped.
    c = _curve(lambda t: np.where((t > 10) & (t < 56.4), 10.0, 0.1))
    segs = detect_segments("x", min_rally_s=2.5, max_clip_s=45.0, curve=c)
    assert segs, "expected at least one segment"
    assert all(s.duration >= 2.5 for s in segs), "a sub-minimum tail fragment survived the split"


def test_constant_motion_is_rejected():
    # Handheld / moving camera: no quiet baseline -> whole video reads active.
    c = _curve(lambda t: np.full_like(t, 5.0))
    assert detect_segments("x", curve=c) == []


def test_normal_two_rallies():
    c = _curve(lambda t: np.where(((t > 10) & (t < 20)) | ((t > 40) & (t < 48)), 10.0, 0.1))
    segs = detect_segments("x", curve=c)
    assert len(segs) == 2


def test_empty_curve():
    assert detect_segments("x", curve=ActivityCurve([], [], 0.0)) == []


def test_fixed_windows_rejects_nonpositive():
    with pytest.raises(ValueError):
        fixed_windows("x", 0)
    with pytest.raises(ValueError):
        fixed_windows("x", -5)


def test_from_file_validates(tmp_path):
    good = tmp_path / "g.json"
    good.write_text("[[1, 5], {\"start_s\": 10, \"end_s\": 20}]")
    segs = from_file(good)
    assert [(s.start_s, s.end_s) for s in segs] == [(1.0, 5.0), (10.0, 20.0)]

    bad = tmp_path / "b.json"
    bad.write_text("[[5, 5]]")  # end <= start
    with pytest.raises(ValueError):
        from_file(bad)

    neg = tmp_path / "n.json"
    neg.write_text("[[-1, 5]]")
    with pytest.raises(ValueError):
        from_file(neg)


def test_segment_duration():
    assert math.isclose(Segment(3.0, 8.5).duration, 5.5)
