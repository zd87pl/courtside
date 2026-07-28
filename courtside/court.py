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
    return {"half": "near" if near else "far", "depth": depth, "lateral": lateral,
            "zone": f"{depth}_{lateral}"}


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
    if H is not None:
        zsum: dict[str, dict[str, int]] = {}
        for r in sr:
            xy = r.get("ankle_px")
            if not xy:
                continue
            court_xy = image_to_court(H, (float(xy[0]), float(xy[1])))
            if court_xy is None:
                continue
            zone = position_zone(*court_xy)
            flagged = any(abs(r["t_s"] - t) < 0.35 for t in flagged_ts)
            quality = (r.get("contact") or {}).get("quality", "acceptable")
            doc["positions"].append({
                "t_s": r["t_s"], "player": r["player"], "stroke": r["stroke"],
                "x_m": court_xy[0], "y_m": court_xy[1],
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
    else:
        doc["court_detected"] = False

    out_path.write_text(json.dumps(doc, indent=2))
    return doc


def build_courtmap(anchor_frame: Path, positions: list[dict[str, Any]],
                   out_path: Path) -> dict[str, Any] | None:
    """positions: [{t_s, player, code, severity, xy: (px, py)}] in anchor-frame pixels."""
    H = detect_court_homography(anchor_frame)
    if H is None:
        return None
    mapped = []
    for p in positions:
        xy = p.get("xy")
        if not xy:
            continue
        court_xy = image_to_court(H, (float(xy[0]), float(xy[1])))
        if court_xy is None:
            continue
        mapped.append({"t_s": p.get("t_s"), "player": p.get("player"),
                       "code": p.get("code"), "severity": p.get("severity"),
                       "x_m": court_xy[0], "y_m": court_xy[1]})
    if not mapped:
        return None
    doc = {"court_w_m": COURT_W, "court_l_m": COURT_L, "positions": mapped,
           "note": "experimental: fixed-camera homography; error positions only"}
    out_path.write_text(json.dumps(doc, indent=2))
    return doc
