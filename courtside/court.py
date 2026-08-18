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
    """Homography image->court meters from the largest white-line quadrilateral."""
    img = cv2.imread(str(frame_path))
    if img is None:
        return None
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


def build_serve_return_map(
    anchor_frame: Path | None,
    records: list[dict[str, Any]],
    flagged_ts: set[float],
    out_path: Path,
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

    H = detect_court_homography(anchor_frame) if anchor_frame and anchor_frame.exists() else None
    doc["court_detected"] = False
    if H is not None:
        candidates = [r for r in sr if r.get("ankle_px")]
        if not candidates:
            # court found; there is simply nothing to place on it
            doc["court_detected"] = True
            doc["note"] += " No player positions could be measured (no ankle keypoints)."
            out_path.write_text(json.dumps(doc, indent=2))
            return doc
        mapped = _map_positions(H, candidates)
        ok, reason = _positions_plausible(mapped, len(candidates))
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
    anchor_frame: Path | None,
    records: list[dict[str, Any]],
    stroke_info: list[dict[str, Any]],
    out_path: Path,
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
    H = detect_court_homography(anchor_frame) if anchor_frame and anchor_frame.exists() else None
    if H is None:
        doc["court_detected"] = False
        out_path.write_text(json.dumps(doc, indent=2))
        return doc
    candidates = [r for r in records if r.get("ankle_px")]
    if not candidates:
        doc["court_detected"] = True
        doc["note"] += " No player positions could be measured (no ankle keypoints)."
        out_path.write_text(json.dumps(doc, indent=2))
        return doc
    mapped = _map_positions(H, candidates)
    ok, reason = _positions_plausible(mapped, len(candidates))
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
