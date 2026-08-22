"""Experimental court mapping: project flagged-moment positions onto a court diagram.

Fixed-camera footage only. Detects the court's outer boundary as the largest
white-line quadrilateral, builds a homography to real court coordinates
(doubles court: 10.97m x 23.77m), and maps player ankle positions from the
flagged moments onto it - "where the errors happened".

Deliberately best-effort: if the boundary isn't found confidently, the whole
feature silently skips. It is behind --heatmap and labeled experimental.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

COURT_W, COURT_L = 10.97, 23.77  # meters, doubles
ALLEY_M = 1.37                   # doubles alley width each side
SINGLES_W = COURT_W - 2 * ALLEY_M  # 8.23m
LANES = ("wide_left", "left", "center", "right", "wide_right")
DEPTHS = ("net", "midcourt", "baseline", "behind_baseline")

# The coach-facing zone x runway template (matches the manual error-tracking
# convention coaches already use): five depth zones numbered 1 (net) to
# 5 (back fence) and five lateral runways C-Left, B-Left, A (center),
# B-Right, C-Right. Runways are the doubles alleys plus singles thirds -
# identical strips to LANES, coach-facing names.
RUNWAY_LABELS = {"wide_left": "C-L", "left": "B-L", "center": "A",
                 "right": "B-R", "wide_right": "C-R"}
RUNWAYS = ("C-L", "B-L", "A", "B-R", "C-R")
ZONE_NAMES = {1: "net", 2: "mid-court", 3: "no-man's land", 4: "baseline", 5: "back"}
_SERVICE_LINE_M = 6.40  # from the net
_NML_END_M = 9.5        # no-man's land ends ~2.4m inside the baseline


def zone_number(dist_from_net: float, depth_from_baseline: float) -> int:
    """Depth zone 1-5 for a player's position (1 = net ... 5 = back fence)."""
    if depth_from_baseline > 0.5:
        return 5
    if dist_from_net <= 2.5:
        return 1
    if dist_from_net <= _SERVICE_LINE_M:
        return 2
    if dist_from_net <= _NML_END_M:
        return 3
    return 4


def lane_for_x(x_m: float) -> str:
    """5 runway lanes from the player's own perspective (x already mirrored for
    the far player). Wide lanes are the doubles alleys and anything beyond;
    the singles width splits into three equal ~2.74m lanes."""
    third = SINGLES_W / 3
    if x_m < ALLEY_M:
        return "wide_left"
    if x_m < ALLEY_M + third:
        return "left"
    if x_m < ALLEY_M + 2 * third:
        return "center"
    if x_m < COURT_W - ALLEY_M:
        return "right"
    return "wide_right"


def detect_court_homography(frame_path: Path) -> np.ndarray | None:
    """Homography image->court meters. Two detectors, cheap first:

    1. largest-white-quadrilateral (works when the whole outer boundary is a
       clean closed loop - synthetic frames, high fixed cameras);
    2. line-model fitting (Hough lines + known court geometry, scored by how
       much of the projected court model lands on white line pixels) - the
       robust path for real footage where the net band hides the far
       baseline, players stand on lines, or lighting varies.
    """
    img = cv2.imread(str(frame_path))
    if img is None:
        return None
    # bounded working resolution; compose the scale back into the homography
    scale = 1.0
    work = img
    if max(img.shape[:2]) > 1600:
        scale = 1600.0 / max(img.shape[:2])
        work = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)),
                          interpolation=cv2.INTER_AREA)
    H = _detect_court_quad(work)
    if H is None:
        H = _detect_court_lines(work)
    if H is None:
        return None
    if scale != 1.0:
        H = H @ np.diag([scale, scale, 1.0])
    return H


def _detect_court_quad(img: np.ndarray) -> np.ndarray | None:
    """Detector 1: the largest white-line quadrilateral."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # white-ish line mask: low saturation, high value
    mask = cv2.inRange(hsv, (0, 0, 170), (180, 80, 255))
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    h, w = img.shape[:2]
    best = max(contours, key=cv2.contourArea)
    if cv2.contourArea(best) < 0.05 * w * h:  # too small to be the court
        return None
    peri = cv2.arcLength(best, True)
    quad = cv2.approxPolyDP(best, 0.02 * peri, True)
    if len(quad) != 4:
        hull = cv2.convexHull(best)
        quad = cv2.approxPolyDP(hull, 0.05 * cv2.arcLength(hull, True), True)
        if len(quad) != 4:
            return None
    pts = quad.reshape(4, 2).astype(np.float32)
    # order: top-left, top-right, bottom-right, bottom-left (image y down)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    ordered = np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                        pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)
    if not cv2.isContourConvex(ordered.astype(np.int32)):
        return None
    # a court seen from a normal camera position projects with the near
    # baseline wider than the far one and meaningful height; razor-thin or
    # inverted quads are detector misfires, not courts
    top_w = float(np.linalg.norm(ordered[1] - ordered[0]))
    bot_w = float(np.linalg.norm(ordered[2] - ordered[3]))
    height = float(abs(ordered[3][1] - ordered[0][1]))
    if max(top_w, bot_w) <= 0 or height < 0.08 * h:
        return None
    if min(top_w, bot_w) / max(top_w, bot_w) < 0.15:
        return None
    # a court's projected height is a substantial fraction of its width even at
    # shallow camera angles; a wide thin band is a banner/strip, not a court
    if height / max(top_w, bot_w) < 0.2:
        return None
    # far baseline at y=0, near baseline at y=COURT_L
    court = np.array([[0, 0], [COURT_W, 0], [COURT_W, COURT_L], [0, COURT_L]],
                     dtype=np.float32)
    H, _ = cv2.findHomography(ordered, court, cv2.RANSAC)
    return H


# model lines as (x0, y0, x1, y1) in court meters; the support score samples
# these and checks the projection lands on white line pixels
_SL = 5.485  # service line distance from its own baseline (23.77/2 - 6.40... no: L/2 - 6.40 = 5.485)
_MODEL_LINES = (
    (0, 0, COURT_W, 0), (0, COURT_L, COURT_W, COURT_L),              # baselines
    (0, 0, 0, COURT_L), (COURT_W, 0, COURT_W, COURT_L),              # doubles sidelines
    (ALLEY_M, 0, ALLEY_M, COURT_L), (COURT_W - ALLEY_M, 0, COURT_W - ALLEY_M, COURT_L),
    (ALLEY_M, _SL, COURT_W - ALLEY_M, _SL),                          # far service line
    (ALLEY_M, COURT_L - _SL, COURT_W - ALLEY_M, COURT_L - _SL),      # near service line
    (COURT_W / 2, _SL, COURT_W / 2, COURT_L - _SL),                  # center service line
)
_MODEL_PTS = np.array([
    [x0 + (x1 - x0) * t, y0 + (y1 - y0) * t]
    for x0, y0, x1, y1 in _MODEL_LINES for t in np.linspace(0.02, 0.98, 25)
], dtype=np.float64)


def _cluster_lines(segs: np.ndarray, w: int) -> list[tuple[float, float, float]]:
    """Merge Hough segments into (theta, rho, total_length) lines."""
    raw = []
    for x1, y1, x2, y2 in segs[:, 0]:
        theta = float(np.arctan2(y2 - y1, x2 - x1)) % np.pi
        rho = float(-x1 * np.sin(theta) + y1 * np.cos(theta))
        raw.append((theta, rho, float(np.hypot(x2 - x1, y2 - y1))))
    clusters: list[list] = []
    for theta, rho, ln in sorted(raw, key=lambda r: -r[2]):
        for c in clusters:
            dt = abs(theta - c[0])
            dt = min(dt, np.pi - dt)
            if dt < np.radians(2.5) and abs(rho - c[1]) < 0.012 * w:
                c[2] += ln
                break
        else:
            clusters.append([theta, rho, ln])
    return [tuple(c) for c in clusters]


def _line_intersect(a: tuple, b: tuple) -> tuple[float, float] | None:
    """Intersection of two (theta, rho) lines with normal n=(-sin t, cos t)."""
    n1 = (-np.sin(a[0]), np.cos(a[0]))
    n2 = (-np.sin(b[0]), np.cos(b[0]))
    det = n1[0] * n2[1] - n1[1] * n2[0]
    if abs(det) < 1e-9:
        return None
    x = (a[1] * n2[1] - b[1] * n1[1]) / det
    y = (n1[0] * b[1] - n2[0] * a[1]) / det
    return x, y


def _detect_court_lines(img: np.ndarray) -> np.ndarray | None:
    """Detector 2: fit the known court geometry to detected line families.

    White-line RIDGE mask (brighter than both side neighbors) -> Hough lines ->
    split into near-horizontal (baselines/service lines) and steeper
    (sidelines) families -> try model assignments for pairs from each family
    -> keep the homography whose full projected court model has the best
    support on the ridge mask. Needs only 4 visible lines, so it survives the
    net band hiding the far baseline, players on lines, and worn paint.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.int16)
    d = max(3, w // 220)
    sh = np.roll
    diff_h = np.minimum(gray - sh(gray, d, axis=1), gray - sh(gray, -d, axis=1))
    diff_v = np.minimum(gray - sh(gray, d, axis=0), gray - sh(gray, -d, axis=0))
    ridge = (((diff_h > 18) | (diff_v > 18)) & (gray > 110)).astype(np.uint8)
    ridge[:d, :] = 0
    ridge[-d:, :] = 0
    ridge[:, :d] = 0
    ridge[:, -d:] = 0
    mask = ridge * 255
    segs = cv2.HoughLinesP(mask, 1, np.pi / 360, threshold=60,
                           minLineLength=int(0.10 * w), maxLineGap=int(0.02 * w))
    if segs is None or len(segs) < 4:
        return None
    lines = _cluster_lines(segs, w)
    def from_horizontal(t: float) -> float:
        return min(t, np.pi - t)
    horiz = sorted([l for l in lines if from_horizontal(l[0]) < np.radians(30)],
                   key=lambda l: -l[2])[:6]
    steep = sorted([l for l in lines if from_horizontal(l[0]) >= np.radians(30)],
                   key=lambda l: -l[2])[:6]
    if len(horiz) < 2 or len(steep) < 2:
        return None

    def y_at_center(l: tuple) -> float:
        # y where the line crosses x = w/2 (horiz family: cos(theta) != 0)
        return (l[1] + (w / 2) * np.sin(l[0])) / np.cos(l[0])

    def x_at(l: tuple, y: float) -> float:
        s = np.sin(l[0])
        if abs(s) < 1e-6:
            return 1e9
        return (y * np.cos(l[0]) - l[1]) / s

    support = cv2.dilate(mask, np.ones((7, 7), np.uint8))
    # (far_y, near_y) model assignments for the upper/lower horizontal pair
    h_pairs = ((0.0, COURT_L), (_SL, COURT_L), (0.0, COURT_L - _SL), (_SL, COURT_L - _SL))
    v_pairs = ((0.0, COURT_W), (ALLEY_M, COURT_W - ALLEY_M))
    best: tuple[float, np.ndarray] | None = None
    from itertools import combinations
    for ha, hb in combinations(horiz, 2):
        top, bot = sorted((ha, hb), key=y_at_center)
        if y_at_center(bot) - y_at_center(top) < 0.10 * h:
            continue
        for va, vb in combinations(steep, 2):
            ymid = (y_at_center(top) + y_at_center(bot)) / 2
            left, right = sorted((va, vb), key=lambda l: x_at(l, ymid))
            if x_at(right, ymid) - x_at(left, ymid) < 0.15 * w:
                continue
            for far_y, near_y in h_pairs:
                for lx, rx in v_pairs:
                    pts_img = [_line_intersect(top, left), _line_intersect(top, right),
                               _line_intersect(bot, right), _line_intersect(bot, left)]
                    if any(p is None for p in pts_img):
                        continue
                    P = np.array(pts_img, dtype=np.float64)
                    if (P[:, 0] < -w).any() or (P[:, 0] > 2 * w).any() \
                            or (P[:, 1] < -h).any() or (P[:, 1] > 2 * h).any():
                        continue
                    model = np.array([[lx, far_y], [rx, far_y], [rx, near_y], [lx, near_y]],
                                     dtype=np.float32)
                    try:
                        Hc = cv2.getPerspectiveTransform(P.astype(np.float32), model)
                        Hinv = np.linalg.inv(Hc)
                    except (cv2.error, np.linalg.LinAlgError):
                        continue
                    proj = cv2.perspectiveTransform(
                        _MODEL_PTS.reshape(-1, 1, 2).astype(np.float64), Hinv).reshape(-1, 2)
                    inside = ((proj[:, 0] >= 0) & (proj[:, 0] < w)
                              & (proj[:, 1] >= 0) & (proj[:, 1] < h))
                    if inside.mean() < 0.65:
                        continue
                    pi = proj[inside].astype(int)
                    hit = support[pi[:, 1], pi[:, 0]] > 0
                    # support on white pixels, small bonus for a centered court
                    center = cv2.perspectiveTransform(
                        np.array([[[COURT_W / 2, COURT_L / 2]]], dtype=np.float64), Hinv)[0, 0]
                    cdist = np.hypot((center[0] - w / 2) / w, (center[1] - h / 2) / h)
                    score = float(hit.mean()) * float(inside.mean()) - 0.08 * float(cdist)
                    if best is None or score > best[0]:
                        best = (score, Hc)
    if best is None or best[0] < 0.45:
        return None
    return best[1]


def homography_from_corners(corners_px: list[tuple[float, float]]) -> np.ndarray:
    """Manual calibration: 4 clicked doubles-court corners, ordered far-left,
    far-right, near-right, near-left, in source-image pixels -> H image->meters."""
    src = np.array(corners_px, dtype=np.float32)
    dst = np.array([[0, 0], [COURT_W, 0], [COURT_W, COURT_L], [0, COURT_L]],
                   dtype=np.float32)
    return cv2.getPerspectiveTransform(src, dst)


def image_to_court(H: np.ndarray, xy: tuple[float, float]) -> tuple[float, float] | None:
    p = cv2.perspectiveTransform(np.array([[xy]], dtype=np.float32), H)[0, 0]
    x, y = float(p[0]), float(p[1])
    margin = 4.0  # allow positions behind the baseline / outside the sideline
    if -margin <= x <= COURT_W + margin and -margin <= y <= COURT_L + margin:
        return round(x, 2), round(y, 2)
    return None


def position_zone(x_m: float, y_m: float) -> dict[str, str]:
    """Named court zone for a player's position, from THAT player's perspective.

    Depth (distance from their own baseline) and lateral third; far-side
    positions are mirrored so 'left' always means the player's left.
    """
    near = y_m > COURT_L / 2
    # distance behind (+) / inside (-) their own baseline
    depth_from_baseline = (y_m - COURT_L) if near else (0.0 - y_m)
    dist_from_net = abs(y_m - COURT_L / 2)
    if depth_from_baseline > 0.5:
        depth = "behind_baseline"
    elif dist_from_net > 6.40:  # behind the service line (6.40m from the net)
        depth = "baseline"
    elif dist_from_net > 2.5:
        depth = "midcourt"
    else:
        depth = "net"
    x = x_m if near else COURT_W - x_m  # mirror for far player
    third = COURT_W / 3
    lateral = "left" if x < third else ("right" if x > 2 * third else "center")
    lane = lane_for_x(x)
    return {"half": "near" if near else "far", "depth": depth, "lateral": lateral,
            "lane": lane, "runway": RUNWAY_LABELS[lane],
            "zone_number": zone_number(dist_from_net, depth_from_baseline),
            "zone": f"{depth}_{lateral}"}


def _map_positions(H: np.ndarray, records: list[dict[str, Any]],
                   ) -> list[tuple[dict[str, Any], float, float, dict[str, str]]]:
    """Project each record's ankle_px through H, keeping only points whose
    computed court half agrees with the VLM's near/far call for that stroke.

    A wrong homography (the white-quad detector latching onto a scoreboard or
    banner) maps every ankle to roughly the same off-court point - the
    half-vs-player check kills those points individually (finding: all court
    dots plotted at one corner on real 4K footage)."""
    out = []
    for r in records:
        xy = r.get("ankle_px")
        if not xy:
            continue
        court_xy = image_to_court(H, (float(xy[0]), float(xy[1])))
        if court_xy is None:
            continue
        zone = position_zone(*court_xy)
        if r.get("player") in ("near", "far") and zone["half"] != r["player"]:
            continue
        out.append((r, court_xy[0], court_xy[1], zone))
    return out


def _positions_plausible(mapped: list, n_candidates: int) -> tuple[bool, str]:
    """Sanity verdict for a set of projected positions.

    Rejects the degenerate signatures of a bad homography: almost no points
    surviving projection, or rally strokes all collapsed onto one spot. The
    collapse check deliberately exempts serve-dominated sets - a player
    serving repeatedly from one station is legitimate footage, and rally
    strokes are what provably require movement (finding: serve-drill sessions
    were falsely rejected as collapsed)."""
    if not mapped:
        return False, "no positions survived projection"
    if n_candidates >= 4 and len(mapped) / n_candidates < 0.4:
        return False, (f"only {len(mapped)}/{n_candidates} positions were "
                       "geometrically consistent")
    non_serve = [m for m in mapped if m[0].get("stroke") != "serve"]
    if len(mapped) >= 4 and len(non_serve) >= 2:
        xs = np.array([m[1] for m in mapped])
        ys = np.array([m[2] for m in mapped])
        if float(xs.std() + ys.std()) < 0.8:
            return False, "all positions collapsed onto one spot"
    return True, ""


def _anchor_list(anchor_frame) -> list[Path]:
    if anchor_frame is None:
        return []
    if isinstance(anchor_frame, (list, tuple)):
        return [Path(a) for a in anchor_frame if a and Path(a).exists()]
    return [Path(anchor_frame)] if Path(anchor_frame).exists() else []


def _resolve_court(anchor_frame, candidates: list[dict[str, Any]],
                   H: "np.ndarray | None" = None):
    """(H, mapped, ok, reason) - first anchor whose projected positions pass
    the plausibility gates wins; a manual/override H skips detection."""
    Hs = [H] if H is not None else \
        [h for h in (detect_court_homography(a) for a in _anchor_list(anchor_frame))
         if h is not None]
    if not Hs:
        return None, [], False, "court not found in any anchor frame"
    last = None
    for Hc in Hs:
        mapped = _map_positions(Hc, candidates)
        ok, reason = _positions_plausible(mapped, len(candidates))
        if ok:
            return Hc, mapped, True, ""
        last = (Hc, mapped, reason)
    return last[0], last[1], False, last[2]


def build_serve_return_map(
    anchor_frame: "Path | list | None",
    records: list[dict[str, Any]],
    flagged_ts: set[float],
    out_path: Path,
    H: "np.ndarray | None" = None,
) -> dict[str, Any]:
    """Serve & return analysis: player position at contact by court zone, with
    per-zone error rates, plus the return-of-serve contact-height distribution.

    Heights need no court detection and are always produced; court positions
    are added when the homography succeeds. This maps WHERE THE PLAYER STOOD
    at contact - serve landing placement would need far-side ball tracking and
    is deliberately not claimed.
    """
    sr = [r for r in records if r.get("stroke") in ("serve", "return")]

    # return-of-serve contact height: zone distribution + quality counts
    heights: dict[str, int] = {}
    hq = {"ideal": 0, "acceptable": 0, "poor": 0}
    for r in sr:
        if r["stroke"] != "return":
            continue
        c = r.get("contact") or {}
        z = c.get("zone")
        if z:
            heights[z] = heights.get(z, 0) + 1
            q = c.get("quality", "acceptable")
            hq[q] = hq.get(q, 0) + 1

    doc: dict[str, Any] = {
        "counts": {
            "serves": sum(1 for r in sr if r["stroke"] == "serve"),
            "returns": sum(1 for r in sr if r["stroke"] == "return"),
        },
        "return_height": {"zones": heights, "quality": hq},
        "positions": [],
        "zones_summary": {},
        "note": ("player position at contact via fixed-camera court homography; "
                 "heights are 2D image-plane zones. Serve LANDING placement is not "
                 "claimed (needs ball tracking)."),
    }

    candidates = [r for r in sr if r.get("ankle_px")]
    Hr, mapped, ok, reason = _resolve_court(anchor_frame, candidates, H=H)
    doc["court_detected"] = False
    if Hr is not None:
        if not candidates:
            # court found; there is simply nothing to place on it
            doc["court_detected"] = True
            doc["note"] += " No player positions could be measured (no ankle keypoints)."
            out_path.write_text(json.dumps(doc, indent=2))
            return doc
        if not ok:
            doc["note"] += f" Court positions dropped: {reason}."
        else:
            zsum: dict[str, dict[str, int]] = {}
            for r, x_m, y_m, zone in mapped:
                flagged = any(abs(r["t_s"] - t) < 0.35 for t in flagged_ts)
                quality = (r.get("contact") or {}).get("quality", "acceptable")
                doc["positions"].append({
                    "t_s": r["t_s"], "player": r["player"], "stroke": r["stroke"],
                    "x_m": x_m, "y_m": y_m,
                    "zone": zone["zone"], "half": zone["half"],
                    "quality": quality, "flagged": flagged,
                })
                key = f'{r["stroke"]}:{zone["zone"]}'
                b = zsum.setdefault(key, {"count": 0, "poor": 0, "flagged": 0})
                b["count"] += 1
                b["poor"] += 1 if quality == "poor" else 0
                b["flagged"] += 1 if flagged else 0
            doc["zones_summary"] = zsum
            doc["court_detected"] = True

    out_path.write_text(json.dumps(doc, indent=2))
    return doc


_BAD_OUTCOMES = ("net", "out_long", "out_wide")


def build_error_matrix(
    anchor_frame: "Path | list | None",
    records: list[dict[str, Any]],
    stroke_info: list[dict[str, Any]],
    out_path: Path,
    H: "np.ndarray | None" = None,
) -> dict[str, Any]:
    """Depth x runway-lane error matrix over ALL measured strokes, per player.

    records: the contact-quality sweep ({t_s, player, stroke, contact, ankle_px}).
    stroke_info: per-stroke VLM data ({t_s, outcome, received, max_severity}),
    matched to records by nearest timestamp within 0.35s.

    A measured stroke counts as an error when any of these fired (each is also
    counted separately per cell so the UI can show the breakdown):
    - the VLM saw the ball go out (outcome in net/out_long/out_wide),
    - it carries a medium+ flag,
    - measured contact quality is "poor".
    Cells are where the player STOOD at contact (fixed-camera homography) -
    same honesty rule as the serve/return map.
    """
    doc: dict[str, Any] = {
        "zones": {str(k): v for k, v in ZONE_NAMES.items()},
        "runways": list(RUNWAYS),
        "players": {}, "worst_cells": [], "positions": [],
        "note": ("zone (1=net .. 5=back) x runway (C-L/B-L/A/B-R/C-R) grid - the "
                 "manual error-tracking template, filled in automatically. Cells "
                 "are where the player STOOD at contact via fixed-camera "
                 "homography; outcome/received are model judgments; flag and "
                 "contact criteria are measured."),
    }
    candidates = [r for r in records if r.get("ankle_px")]
    Hr, mapped, ok, reason = _resolve_court(anchor_frame, candidates, H=H)
    if Hr is None:
        doc["court_detected"] = False
        out_path.write_text(json.dumps(doc, indent=2))
        return doc
    if not candidates:
        doc["court_detected"] = True
        doc["note"] += " No player positions could be measured (no ankle keypoints)."
        out_path.write_text(json.dumps(doc, indent=2))
        return doc
    if not ok:
        doc["court_detected"] = False
        doc["note"] += f" Positions dropped: {reason}."
        out_path.write_text(json.dumps(doc, indent=2))
        return doc
    doc["court_detected"] = True

    def info_for(t: float) -> dict[str, Any]:
        best, best_dt = None, 0.35
        for si in stroke_info:
            dt = abs(float(si.get("t_s", -1e9)) - t)
            if dt < best_dt:
                best, best_dt = si, dt
        return best or {}

    sev_rank = {"low": 1, "medium": 2, "high": 3}
    for r, x_m, y_m, zone in mapped:
        court_xy = (x_m, y_m)
        cell = f'z{zone["zone_number"]}:{zone["runway"]}'
        si = info_for(float(r["t_s"]))
        outcome = si.get("outcome", "unknown")
        quality = (r.get("contact") or {}).get("quality", "acceptable")
        causes: list[str] = []
        if outcome in _BAD_OUTCOMES:
            causes.append(outcome)
        if sev_rank.get(si.get("max_severity") or "", 0) >= 2:
            causes.append("flag")
        if quality == "poor":
            causes.append("poor_contact")

        player = r.get("player", "unknown")
        pdoc = doc["players"].setdefault(player, {"measured": 0, "errors": 0, "cells": {}})
        c = pdoc["cells"].setdefault(cell, {"measured": 0, "errors": 0, "net": 0,
                                            "out_long": 0, "out_wide": 0,
                                            "flagged": 0, "poor_contact": 0})
        pdoc["measured"] += 1
        c["measured"] += 1
        if causes:
            pdoc["errors"] += 1
            c["errors"] += 1
            if outcome in _BAD_OUTCOMES:
                c[outcome] += 1
            if "flag" in causes:
                c["flagged"] += 1
            if "poor_contact" in causes:
                c["poor_contact"] += 1
            doc["positions"].append({
                "t_s": r["t_s"], "player": player, "stroke": r.get("stroke"),
                "x_m": court_xy[0], "y_m": court_xy[1], "cell": cell,
                "zone_number": zone["zone_number"], "runway": zone["runway"],
                "causes": causes,
            })

    worst = [{"player": p, "cell": cell, "errors": c["errors"], "measured": c["measured"]}
             for p, pdoc in doc["players"].items() for cell, c in pdoc["cells"].items()
             if c["errors"]]
    worst.sort(key=lambda w: (-w["errors"], w["measured"]))
    doc["worst_cells"] = worst[:8]

    out_path.write_text(json.dumps(doc, indent=2))
    return doc


def build_courtmap(anchor_frame: Path, positions: list[dict[str, Any]],
                   out_path: Path) -> dict[str, Any] | None:
    """positions: [{t_s, player, code, severity, xy: (px, py)}] in anchor-frame pixels."""
    H = detect_court_homography(anchor_frame)
    if H is None:
        return None
    candidates = [dict(p, ankle_px=p.get("xy")) for p in positions if p.get("xy")]
    projected = _map_positions(H, candidates)
    ok, _reason = _positions_plausible(projected, len(candidates))
    if not ok:
        return None
    mapped = [{"t_s": p.get("t_s"), "player": p.get("player"),
               "code": p.get("code"), "severity": p.get("severity"),
               "x_m": x_m, "y_m": y_m}
              for p, x_m, y_m, _zone in projected]
    if not mapped:
        return None
    doc = {"court_w_m": COURT_W, "court_l_m": COURT_L, "positions": mapped,
           "note": "experimental: fixed-camera homography; error positions only"}
    out_path.write_text(json.dumps(doc, indent=2))
    return doc
