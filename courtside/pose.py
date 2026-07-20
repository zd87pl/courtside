"""On-device biomechanics: 2D pose, joint angles, overlays, and ghost comparison.

Runs a YOLO pose model (optional ``[pose]`` extra, ultralytics) over short
windows around flagged strokes - never the whole video - and turns COCO-17
keypoints into the measurements coaches actually use: elbow/knee angles,
hip-shoulder separation, stance width, contact height. Draws skeleton +
angle-badge overlays with OpenCV (already a core dependency).

Honesty guardrail (matches the report's limitations section): these are
IMAGE-PLANE angles from monocular video. They support form observations -
never force, weight-transfer, or absolute-distance claims. Every rendered
overlay is labeled "2D image-plane".

Keypoint order (COCO-17): 0 nose, 1-2 eyes, 3-4 ears, 5 L-shoulder,
6 R-shoulder, 7 L-elbow, 8 R-elbow, 9 L-wrist, 10 R-wrist, 11 L-hip,
12 R-hip, 13 L-knee, 14 R-knee, 15 L-ankle, 16 R-ankle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

CONF_MIN = 0.5  # keypoint confidence gate: below this an angle is not reported

L_SHO, R_SHO, L_ELB, R_ELB, L_WRI, R_WRI = 5, 6, 7, 8, 9, 10
L_HIP, R_HIP, L_KNE, R_KNE, L_ANK, R_ANK = 11, 12, 13, 14, 15, 16

_SKELETON = [  # bone pairs for drawing
    (L_SHO, R_SHO), (L_SHO, L_ELB), (L_ELB, L_WRI), (R_SHO, R_ELB), (R_ELB, R_WRI),
    (L_SHO, L_HIP), (R_SHO, R_HIP), (L_HIP, R_HIP),
    (L_HIP, L_KNE), (L_KNE, L_ANK), (R_HIP, R_KNE), (R_KNE, R_ANK),
]


def pose_available() -> bool:
    try:
        import ultralytics  # noqa: F401
        return True
    except ImportError:
        return False


@dataclass
class FramePose:
    """One person's keypoints in one frame: (17, 3) array of x, y, conf."""
    kpts: np.ndarray
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2


@dataclass
class PoseWindow:
    """Pose track for the subject player across a flag window."""
    frame_paths: list[Path]
    fps: float
    start_s: float                       # absolute time of first frame
    poses: list[FramePose | None]        # one entry per frame (None = not found)
    contact_idx: int = 0                 # frame index of (refined) contact
    angles: dict[str, Any] = field(default_factory=dict)

    @property
    def contact_t_s(self) -> float:
        return self.start_s + self.contact_idx / self.fps if self.fps > 0 else self.start_s


# ---------------- geometry ----------------

def _pt(kpts: np.ndarray, i: int) -> np.ndarray | None:
    if kpts[i, 2] < CONF_MIN:
        return None
    return kpts[i, :2]


def _angle_at(kpts: np.ndarray, a: int, vertex: int, b: int) -> float | None:
    """Interior angle (degrees) at `vertex` between rays to a and b."""
    pa, pv, pb = _pt(kpts, a), _pt(kpts, vertex), _pt(kpts, b)
    if pa is None or pv is None or pb is None:
        return None
    v1, v2 = pa - pv, pb - pv
    n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return None
    cosang = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))
    return math.degrees(math.acos(cosang))


def _line_deg(kpts: np.ndarray, i: int, j: int) -> float | None:
    p, q = _pt(kpts, i), _pt(kpts, j)
    if p is None or q is None:
        return None
    return math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))


def torso_len(kpts: np.ndarray) -> float | None:
    """Shoulder-center to hip-center distance - the normalization unit."""
    pts = [_pt(kpts, i) for i in (L_SHO, R_SHO, L_HIP, R_HIP)]
    if any(p is None for p in pts):
        return None
    sho = (pts[0] + pts[1]) / 2
    hip = (pts[2] + pts[3]) / 2
    d = float(np.linalg.norm(sho - hip))
    return d if d > 1e-3 else None


def compute_angles(kpts: np.ndarray) -> dict[str, Any]:
    """Coach-relevant 2D angles from one frame's keypoints. None = occluded."""
    out: dict[str, Any] = {
        "elbow_left_deg": _angle_at(kpts, L_SHO, L_ELB, L_WRI),
        "elbow_right_deg": _angle_at(kpts, R_SHO, R_ELB, R_WRI),
        "knee_left_deg": _angle_at(kpts, L_HIP, L_KNE, L_ANK),
        "knee_right_deg": _angle_at(kpts, R_HIP, R_KNE, R_ANK),
    }
    sho_line, hip_line = _line_deg(kpts, L_SHO, R_SHO), _line_deg(kpts, L_HIP, R_HIP)
    if sho_line is not None and hip_line is not None:
        sep = abs(sho_line - hip_line) % 180
        out["hip_shoulder_separation_deg"] = min(sep, 180 - sep)
    else:
        out["hip_shoulder_separation_deg"] = None
    la, ra = _pt(kpts, L_ANK), _pt(kpts, R_ANK)
    ls, rs = _pt(kpts, L_SHO), _pt(kpts, R_SHO)
    if la is not None and ra is not None and ls is not None and rs is not None:
        sw = float(np.linalg.norm(ls - rs))
        out["stance_width_ratio"] = round(float(np.linalg.norm(la - ra)) / sw, 2) if sw > 1e-3 else None
    else:
        out["stance_width_ratio"] = None
    # contact height: 0 = hip height, 1 = shoulder height, >1 above shoulders
    lw, rw = _pt(kpts, L_WRI), _pt(kpts, R_WRI)
    lh, rh = _pt(kpts, L_HIP), _pt(kpts, R_HIP)
    if all(p is not None for p in (ls, rs, lh, rh)) and (lw is not None or rw is not None):
        sho_y = (ls[1] + rs[1]) / 2
        hip_y = (lh[1] + rh[1]) / 2
        span = hip_y - sho_y
        if abs(span) > 1e-3:
            wri_y = min(w[1] for w in (lw, rw) if w is not None)  # higher wrist
            out["contact_height_ratio"] = round(float((hip_y - wri_y) / span), 2)
        else:
            out["contact_height_ratio"] = None
    else:
        out["contact_height_ratio"] = None
    for k, v in list(out.items()):
        if isinstance(v, float):
            out[k] = round(v, 1)
    return out


# ---------------- subject selection + tracking ----------------

def pick_subject(people: list[FramePose], player: str, frame_h: float) -> FramePose | None:
    """Choose the flagged player: 'near' = biggest/lowest box, 'far' = highest."""
    if not people:
        return None
    def area(p: FramePose) -> float:
        x1, y1, x2, y2 = p.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if player == "far":
        return min(people, key=lambda p: p.bbox[3])  # smallest bottom-y = farthest
    # near/unknown: the dominant figure - biggest area, tie-broken by lower bottom
    return max(people, key=lambda p: (area(p), p.bbox[3]))


def wrist_speeds(poses: list[FramePose | None]) -> list[float]:
    """Per-frame max wrist speed, normalized by torso length (units: torso/frame)."""
    speeds = [0.0] * len(poses)
    for i in range(1, len(poses)):
        a, b = poses[i - 1], poses[i]
        if a is None or b is None:
            continue
        t = torso_len(b.kpts) or torso_len(a.kpts)
        if not t:
            continue
        best = 0.0
        for w in (L_WRI, R_WRI):
            p0, p1 = _pt(a.kpts, w), _pt(b.kpts, w)
            if p0 is None or p1 is None:
                continue
            best = max(best, float(np.linalg.norm(p1 - p0)) / t)
        speeds[i] = round(best, 3)
    return speeds


def refine_contact_idx(poses: list[FramePose | None], nominal_idx: int,
                       max_shift: int = 6) -> int:
    """Snap the contact frame to the wrist-speed peak near the nominal index."""
    speeds = wrist_speeds(poses)
    lo = max(0, nominal_idx - max_shift)
    hi = min(len(speeds), nominal_idx + max_shift + 1)
    window = speeds[lo:hi]
    if not window or max(window) <= 0:
        return nominal_idx
    return lo + int(np.argmax(window))


# ---------------- the model ----------------

def ankle_midpoint(pose: FramePose) -> tuple[float, float] | None:
    """Ground-contact proxy for court positioning."""
    la, ra = _pt(pose.kpts, L_ANK), _pt(pose.kpts, R_ANK)
    if la is None and ra is None:
        return None
    if la is None or ra is None:
        p = la if la is not None else ra
        return float(p[0]), float(p[1])
    return float((la[0] + ra[0]) / 2), float((la[1] + ra[1]) / 2)


class PoseEstimator:
    """Thin wrapper over YOLO pose; loads once, reused across moments."""

    def __init__(self, model_name: str | None = None):
        import os

        from ultralytics import YOLO  # deferred: optional heavy import
        self.model = YOLO(model_name or os.environ.get("COURTSIDE_POSE_MODEL", "yolo11n-pose.pt"))

    def detect(self, frame_path: Path) -> list[FramePose]:
        results = self.model.predict(str(frame_path), verbose=False)
        people: list[FramePose] = []
        for r in results:
            if r.keypoints is None or r.boxes is None:
                continue
            kdata = r.keypoints.data.cpu().numpy() if hasattr(r.keypoints.data, "cpu") \
                else np.asarray(r.keypoints.data)
            boxes = r.boxes.xyxy.cpu().numpy() if hasattr(r.boxes.xyxy, "cpu") \
                else np.asarray(r.boxes.xyxy)
            for k, b in zip(kdata, boxes):
                if k.shape[0] < 17:
                    continue
                people.append(FramePose(kpts=k.astype(np.float32),
                                        bbox=tuple(float(x) for x in b[:4])))
        return people

    def track_window(self, frame_paths: list[Path], player: str,
                     fps: float, start_s: float, nominal_idx: int) -> PoseWindow:
        poses: list[FramePose | None] = []
        frame_h = 0.0
        for fp in frame_paths:
            people = self.detect(fp)
            if frame_h == 0.0 and people:
                frame_h = max(p.bbox[3] for p in people)
            poses.append(pick_subject(people, player, frame_h))
        win = PoseWindow(frame_paths=frame_paths, fps=fps, start_s=start_s, poses=poses)
        win.contact_idx = refine_contact_idx(poses, nominal_idx)
        contact = poses[win.contact_idx] if win.contact_idx < len(poses) else None
        win.angles = compute_angles(contact.kpts) if contact is not None else {}
        return win


# ---------------- drawing ----------------

_BONE = (240, 200, 60)      # BGR: courtside accent-ish
_JOINT = (255, 255, 255)
_GHOST = (200, 160, 90)


def _draw_skeleton(img: np.ndarray, kpts: np.ndarray, color, thickness: int = 3,
                   alpha: float = 1.0) -> None:
    layer = img if alpha >= 1.0 else img.copy()
    for i, j in _SKELETON:
        p, q = _pt(kpts, i), _pt(kpts, j)
        if p is None or q is None:
            continue
        cv2.line(layer, tuple(p.astype(int)), tuple(q.astype(int)), color, thickness, cv2.LINE_AA)
    for i in range(5, 17):
        p = _pt(kpts, i)
        if p is not None:
            cv2.circle(layer, tuple(p.astype(int)), thickness + 1, _JOINT, -1, cv2.LINE_AA)
    if alpha < 1.0:
        cv2.addWeighted(layer, alpha, img, 1 - alpha, 0, img)


def _badge(img: np.ndarray, text: str, org: tuple[int, int]) -> None:
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
    x, y = org
    cv2.rectangle(img, (x - 4, y - th - 6), (x + tw + 4, y + 4), (20, 20, 20), -1)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)


def draw_overlay(frame_path: Path, pose: FramePose, angles: dict[str, Any],
                 out_path: Path, ghost_kpts: np.ndarray | None = None) -> Path:
    """Skeleton + angle badges (+ optional normalized ghost skeleton)."""
    img = cv2.imread(str(frame_path))
    if img is None:
        raise RuntimeError(f"could not read frame {frame_path}")
    if ghost_kpts is not None:
        _draw_skeleton(img, ghost_kpts, _GHOST, thickness=2, alpha=0.55)
    _draw_skeleton(img, pose.kpts, _BONE)

    badges = []
    for key, label in (("elbow_right_deg", "R elbow"), ("elbow_left_deg", "L elbow"),
                       ("knee_right_deg", "R knee"), ("knee_left_deg", "L knee"),
                       ("hip_shoulder_separation_deg", "hip-shoulder"),
                       ("contact_height_ratio", "contact ht")):
        v = angles.get(key)
        if v is None:
            continue
        badges.append(f"{label}: {v:g}" + ("deg" if key.endswith("_deg") else ""))
    y = 26
    for b in badges[:5]:
        _badge(img, b, (10, y))
        y += 24
    _badge(img, "2D image-plane estimate", (10, img.shape[0] - 10))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out_path


def normalize_to(anchor_kpts: np.ndarray, ref_kpts: np.ndarray) -> np.ndarray | None:
    """Map a reference skeleton onto the anchor player's hip-center + torso scale."""
    ta, tr = torso_len(anchor_kpts), torso_len(ref_kpts)
    if not ta or not tr:
        return None
    a_hip = (_pt(anchor_kpts, L_HIP), _pt(anchor_kpts, R_HIP))
    r_hip = (_pt(ref_kpts, L_HIP), _pt(ref_kpts, R_HIP))
    if any(p is None for p in (*a_hip, *r_hip)):
        return None
    a_c = (a_hip[0] + a_hip[1]) / 2
    r_c = (r_hip[0] + r_hip[1]) / 2
    scale = ta / tr
    out = ref_kpts.copy()
    out[:, :2] = (ref_kpts[:, :2] - r_c) * scale + a_c
    return out


def render_overlay_video(frame_paths: list[Path], poses: list[FramePose | None],
                         out_path: Path, fps_out: float = 8.0,
                         ghost_seq: list[np.ndarray | None] | None = None) -> Path | None:
    """Encode skeleton-annotated frames as a slow-motion mp4 (via ffmpeg)."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        n = 0
        for i, (fp, pose) in enumerate(zip(frame_paths, poses)):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            if ghost_seq is not None and i < len(ghost_seq) and ghost_seq[i] is not None:
                _draw_skeleton(img, ghost_seq[i], _GHOST, thickness=2, alpha=0.55)
            if pose is not None:
                _draw_skeleton(img, pose.kpts, _BONE)
            _badge(img, "2D image-plane estimate", (10, img.shape[0] - 10))
            cv2.imwrite(str(Path(td) / f"f_{n:04d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
            n += 1
        if n == 0:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-framerate", f"{fps_out:.2f}", "-i", str(Path(td) / "f_%04d.jpg"),
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out_path)]
        try:
            subprocess.run(cmd, check=True)
        except (subprocess.CalledProcessError, OSError):
            return None
    return out_path if out_path.exists() else None
