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
