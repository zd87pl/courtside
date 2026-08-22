"""Distant-camera 4K fixes: localized motion, auto-crop, homography sanity."""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from courtside.court import (COURT_L, COURT_W, _map_positions, _positions_plausible,
                             build_serve_return_map, detect_court_homography)
from courtside.frames import extract_clip_frames
from courtside.segment import (ActivityCurve, Segment, _activity_curve,
                               detect_segments, motion_crop_box)


# ---------------- localized motion on distant-player footage ----------------

def _write_video(path, frames, fps=10):
    h, w = frames[0].shape[:2]
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps),
         "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         str(path)],
        stdin=subprocess.PIPE)
    for f in frames:
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0


def _distant_player_video(path, dur_s=10, fps=10, size=(640, 360), active=(3.0, 7.0)):
    """Static wide shot + faint sensor noise; a tiny 'player' blob moves only
    during the active window - the regime where whole-frame mean motion fails."""
    rng = np.random.default_rng(7)
    w, h = size
    base = np.full((h, w, 3), 90, dtype=np.uint8)
    frames = []
    for i in range(dur_s * fps):
        t = i / fps
        f = base.copy()
        noise = rng.integers(-3, 4, size=(h, w, 1), dtype=np.int16)
        f = np.clip(f.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        if active[0] <= t <= active[1]:
            x = int(200 + 120 * ((t - active[0]) / (active[1] - active[0])))
            y = 180 + int(18 * np.sin(t * 9))
            f[y:y + 14, x:x + 8] = (250, 250, 250)  # ~0.05% of the pixels
        frames.append(f)
    _write_video(path, frames, fps)


def test_distant_player_motion_is_detected(tmp_path):
    vid = tmp_path / "distant.mp4"
    _distant_player_video(vid, active=(3.0, 7.0))
    curve = _activity_curve(vid)
    segs = detect_segments(vid, min_rally_s=2.0, curve=curve)
    assert segs, "small distant mover produced no segments"
    assert any(s.start_s <= 4.0 and s.end_s >= 6.0 for s in segs), \
        f"active window not covered: {[(s.start_s, s.end_s) for s in segs]}"
    covered = sum(s.duration for s in segs)
    assert covered <= 8.0, "quiet time was not removed"
    assert curve.motion_map is not None and curve.motion_map.max() > 0


def test_motion_crop_zooms_to_active_region(tmp_path):
    vid = tmp_path / "distant.mp4"
    _distant_player_video(vid)
    curve = _activity_curve(vid)
    box = motion_crop_box(curve, 3840, 2160)  # pretend the source was 4K
    assert box is not None
    x, y, w, h = box
    assert w >= 0.40 * 3840 and h >= 0.40 * 2160  # never a sliver
    assert w * h < 0.80 * 3840 * 2160             # but a real zoom
    # the moving blob lives around x in [200, 328] of 640 -> scaled center inside crop
    assert x <= (260 / 640) * 3840 <= x + w


def test_motion_crop_declines_when_motion_fills_frame():
    mm = np.ones((90, 160), dtype=np.float32)
    c = ActivityCurve(times=[0.0], scores=[1.0], duration=1.0, motion_map=mm)
    assert motion_crop_box(c, 1920, 1080) is None
    assert motion_crop_box(ActivityCurve([], [], 0.0, None), 1920, 1080) is None


# ---------------- homography sanity ----------------

def _court_anchor(tmp_path):
    import cv2
    img = np.full((540, 960, 3), (40, 120, 45), dtype=np.uint8)
    cv2.rectangle(img, (150, 90), (810, 480), (255, 255, 255), 8)
    anchor = tmp_path / "anchor.jpg"
    cv2.imwrite(str(anchor), img)
    return anchor


def _rec(t, stroke, player, ankle):
    return {"t_s": t, "player": player, "stroke": stroke,
            "contact": {"zone": "hip_to_chest", "quality": "ideal",
                        "method": "wrist_proxy", "confidence": "low", "height_ratio": 0.5},
            "ankle_px": ankle}


def test_half_mismatched_points_are_dropped(tmp_path):
    anchor = _court_anchor(tmp_path)
    H = detect_court_homography(anchor)
    assert H is not None
    recs = [
        _rec(1, "serve", "near", [480, 450]),   # bottom of court: near half - kept
        _rec(3, "return", "far", [480, 120]),   # top of court: far half - kept
        _rec(5, "forehand", "near", [480, 120]),  # near player mapped to far half - dropped
    ]
    mapped = _map_positions(H, recs)
    assert len(mapped) == 2
    assert {m[0]["stroke"] for m in mapped} == {"serve", "return"}


def test_collapsed_positions_fail_plausibility():
    same = [({"player": "near"}, 1.0, 20.0, {"half": "near"}) for _ in range(6)]
    ok, reason = _positions_plausible(same, 6)
    assert not ok and "collapsed" in reason
    spread = [({"player": "near"}, x, y, {"half": "near"})
              for x, y in ((1.0, 20.0), (6.0, 22.0), (9.0, 18.5), (3.5, 23.5))]
    ok, _ = _positions_plausible(spread, 4)
    assert ok
    ok, reason = _positions_plausible(spread[:1], 10)  # 1 of 10 survived
    assert not ok


def test_serve_return_map_rejects_bad_homography_output(tmp_path):
    """All ankles at one pixel -> every position collapses to one court spot ->
    the map must degrade to court_not_detected instead of plotting garbage."""
    anchor = _court_anchor(tmp_path)
    recs = [_rec(t, "serve" if t % 2 else "return", "near", [480, 450])
            for t in range(1, 7)]
    doc = build_serve_return_map(anchor, recs, set(), tmp_path / "sr.json")
    assert doc["court_detected"] is False
    assert doc["positions"] == []
    assert "collapsed" in doc["note"] or "dropped" in doc["note"].lower()
    # counts and heights still work without the court
    assert doc["counts"]["serves"] + doc["counts"]["returns"] == 6


def test_serve_return_map_still_works_with_plausible_spread(tmp_path):
    anchor = _court_anchor(tmp_path)
    recs = [
        _rec(1, "serve", "near", [300, 460]),
        _rec(3, "serve", "near", [500, 440]),
        _rec(5, "return", "near", [650, 470]),
        _rec(7, "return", "far", [480, 120]),
        _rec(9, "serve", "far", [350, 140]),
    ]
    doc = build_serve_return_map(anchor, recs, set(), tmp_path / "sr.json")
    assert doc["court_detected"] is True
    assert len(doc["positions"]) >= 4
    xs = {p["x_m"] for p in doc["positions"]}
    assert len(xs) > 1  # genuinely spread, not one spot


def test_degenerate_white_quad_is_rejected(tmp_path):
    """A razor-thin white strip (banner/scoreboard edge) must not become a court."""
    import cv2
    img = np.full((540, 960, 3), (40, 120, 45), dtype=np.uint8)
    cv2.rectangle(img, (50, 250), (910, 290), (255, 255, 255), -1)  # wide thin strip
    p = tmp_path / "strip.jpg"
    cv2.imwrite(str(p), img)
    assert detect_court_homography(p) is None


# ---------------- crop plumbing ----------------

def test_serve_drill_from_one_spot_is_accepted(tmp_path):
    """A player serving repeatedly from one station is legitimate footage -
    the collapse gate must only fire on rally strokes (review finding)."""
    anchor = _court_anchor(tmp_path)
    recs = [_rec(t, "serve", "near", [480 + t, 450]) for t in range(1, 7)]
    doc = build_serve_return_map(anchor, recs, set(), tmp_path / "sr.json")
    assert doc["court_detected"] is True
    assert len(doc["positions"]) == 6


def test_valid_court_with_no_ankles_stays_detected(tmp_path):
    """Pose ankles unavailable on every stroke: the court WAS found - report
    that honestly instead of 'court not detected' (review finding)."""
    anchor = _court_anchor(tmp_path)
    recs = [{"t_s": t, "player": "near", "stroke": "serve",
             "contact": {"zone": "hip_to_chest", "quality": "ideal",
                         "method": "wrist_proxy", "confidence": "low",
                         "height_ratio": 0.5}} for t in (1, 3, 5)]
    doc = build_serve_return_map(anchor, recs, set(), tmp_path / "sr.json")
    assert doc["court_detected"] is True
    assert doc["positions"] == []
    assert "No player positions" in doc["note"]


def test_mostly_idle_video_yields_tight_segments():
    """A few rallies in a long quiet video: thresholds must separate real
    activity from the noise bulk instead of chattering (review finding)."""
    rng = np.random.default_rng(11)
    dt = 0.2
    t = np.arange(0, 600, dt)
    scores = rng.normal(2.0, 0.25, size=len(t))
    scores[(t > 100) & (t < 130)] = 10.0
    c = ActivityCurve(times=list(t), scores=list(scores), duration=600.0)
    segs = detect_segments("x", curve=c)
    covered = sum(s.duration for s in segs)
    assert segs, "the real burst was missed"
    assert covered < 60, f"noise chatter produced {covered:.0f}s of junk segments"
    assert any(s.start_s < 105 and s.end_s > 125 for s in segs)


def test_probe_resolution_honors_rotation(tmp_path):
    from courtside.frames import probe_resolution
    vid = tmp_path / "plain.mp4"
    _distant_player_video(vid, dur_s=1)
    assert probe_resolution(vid) == (640, 360)
    rot = tmp_path / "rot.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-display_rotation", "90", "-i", str(vid), "-c", "copy", str(rot)],
                   check=True)
    assert probe_resolution(rot) == (360, 640)


def test_extract_clip_frames_applies_crop(tmp_path):
    import cv2
    vid = tmp_path / "src.mp4"
    _distant_player_video(vid, dur_s=3, active=(0.5, 2.5))
    cf = extract_clip_frames(vid, Segment(0.0, 2.0), tmp_path / "frames",
                             fps=2.0, max_frames=4, max_side=320,
                             crop=(100, 60, 320, 180))
    assert cf.frames
    h, w = cv2.imread(str(cf.frames[0])).shape[:2]
    assert (w, h) == (320, 180)


def test_motion_crop_ignores_weak_stray_clusters():
    """A walker or adjacent-court mover must not stretch the crop box until
    cropping is declined (review of real multi-court footage)."""
    mm = np.zeros((216, 384), dtype=np.float32)
    mm[60:150, 120:260] = 10.0     # dominant play area (our court)
    mm[5:9, 370:378] = 1.0         # stray corner mover, tiny mass
    c = ActivityCurve(times=[0.0], scores=[1.0], duration=1.0, motion_map=mm)
    box = motion_crop_box(c, 3840, 2160)
    assert box is not None
    x, y, w, h = box
    # the stray top-right corner cluster is excluded from the box
    assert x + w < 3700 and y <= (60 / 216) * 2160 + 300
