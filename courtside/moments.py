"""Flagged-moment deep dives: the coaching cards that make the report land.

For the top flagged strokes of a session this module produces, per moment:
  - a slow-motion error clip           (always; ffmpeg)
  - biomechanics: pose track, image-plane joint angles, a skeleton-overlay
    hero frame and an annotated slow-mo video   (when the [pose] extra is
    installed; contact time is snapped to the wrist-speed peak)
  - a ghost comparison against the player's own best same-type stroke from
    this session                        (when pose + a clean reference exist)
  - a VLM coaching card: what happened / why it matters / the correction /
    a measurable target / one drill    (when a VLM is available; measured
    angles are injected into the prompt - the production AceLens pattern of
    the VLM reading CV-stack JSON, not raw pixels alone)

Everything is best-effort and layered: each missing capability degrades the
moment, never the run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .frames import extract_window_frames, slowmo_snippet
from .schema import ClipAnalysis, CoachingCard, coaching_card_schema
from .vlm import extract_json

_SEV_RANK = {"low": 1, "medium": 2, "high": 3}

DEEPDIVE_TEMPLATE = """You are an expert tennis coach doing a focused review of ONE stroke that was
flagged during video analysis. You are looking at {n_frames} consecutive frames covering
{window_s:.1f}s around the moment of contact (t={t_s:.1f}s in the full video).

Flag under review: "{code}" (severity: {severity}, {kind})
Original evidence note: {evidence}
Stroke: {stroke} by the {player} player.

{angles_block}

Write a coaching card for this exact moment. Rules:
- Ground every claim in what is visible in the frames{angles_hint}.
- The correction must be one concrete change, not a list.
- The target must be measurable or checkable by the player themselves
  (an angle range, a timing cue like "racquet back before the bounce", a contact-point cue).
- Monocular video: never claim forces, weight transfer percentages, or absolute distances.

Return ONLY a JSON object matching this schema (no markdown fences, no commentary):
{schema}
"""

ANGLES_BLOCK = """Measured 2D image-plane biomechanics at contact (from on-device pose estimation;
treat as approximate, and as the authoritative numbers - do not invent others):
{angles_json}"""


@dataclass
class MomentAssets:
    """Filenames are relative to the session out_dir (for portable session.json)."""
    slowmo: str | None = None
    overlay: str | None = None
    overlay_video: str | None = None
    ghost_video: str | None = None


def select_flagged_strokes(analyses: list[ClipAnalysis], cap: int = 6) -> list[dict[str, Any]]:
    """The strokes worth a deep dive: highest severity first, capped."""
    picked: list[dict[str, Any]] = []
    for a in analyses:
        for s in a.strokes:
            for kind, flags in (("technique", s.technique_flags), ("tactical", s.tactical_flags)):
                for f in flags:
                    if _SEV_RANK.get(f.severity, 0) >= 2:  # medium+
                        picked.append({
                            "t_s": s.t_s, "stroke": s.stroke, "player": s.player,
                            "code": f.code, "severity": f.severity, "kind": kind,
                            "evidence": f.evidence,
                        })
    picked.sort(key=lambda m: (-_SEV_RANK.get(m["severity"], 0), m["t_s"]))
    # one deep dive per stroke moment: drop same-timestamp duplicates beyond the worst flag
    seen: set[float] = set()
    out = []
    for m in picked:
        if m["t_s"] in seen:
            continue
        seen.add(m["t_s"])
        out.append(m)
    return out[:cap]


def find_reference_stroke(analyses: list[ClipAnalysis], stroke_type: str,
                          player: str, exclude_t: float) -> float | None:
    """The player's own best same-type stroke: unflagged, from this session."""
    for a in analyses:
        for s in a.strokes:
            if (s.stroke == stroke_type and s.player == player
                    and abs(s.t_s - exclude_t) > 1.0
                    and not s.technique_flags and not s.tactical_flags):
                return s.t_s
    return None


def _deepdive_card(vlm, moment: dict[str, Any], frames: list[Path], window_s: float,
                   angles: dict[str, Any] | None, max_tokens: int = 900) -> dict[str, Any] | None:
    """One focused VLM pass -> validated CoachingCard dict (None on failure)."""
    schema = coaching_card_schema()
    if angles:
        usable = {k: v for k, v in angles.items() if v is not None}
        angles_block = ANGLES_BLOCK.format(angles_json=json.dumps(usable)) if usable else ""
    else:
        angles_block = ""
    prompt = DEEPDIVE_TEMPLATE.format(
        n_frames=len(frames), window_s=window_s, t_s=moment["t_s"],
        code=moment["code"], severity=moment["severity"], kind=moment["kind"],
        evidence=moment["evidence"], stroke=moment["stroke"], player=moment["player"],
        angles_block=angles_block,
        angles_hint=" and the measured angles above" if angles_block else "",
        schema=json.dumps(schema),
    )
    try:
        text, _ = vlm.generate(prompt, images=frames, max_tokens=max_tokens,
                               temperature=0.0, json_schema=schema)
        return CoachingCard.model_validate(extract_json(text)).model_dump()
    except Exception:  # noqa: BLE001 - a failed card must not kill the run
        return None


def build_moments(
    video: Path,
    analyses: list[ClipAnalysis],
    out_dir: Path,
    vlm=None,
    cap: int = 6,
    use_pose: bool = True,
    smooth_slowmo: bool = False,
    log=print,
) -> list[dict[str, Any]]:
    """Produce deep-dive assets + cards for the session's top flagged strokes."""
    selected = select_flagged_strokes(analyses, cap=cap)
    if not selected:
        return []
    mdir = out_dir / "moments"
    mdir.mkdir(parents=True, exist_ok=True)

    estimator = None
    if use_pose:
        from .pose import pose_available
        if pose_available():
            from .pose import PoseEstimator
            try:
                estimator = PoseEstimator()
                log("  biomechanics: pose model loaded")
            except Exception as e:  # noqa: BLE001
                log(f"  biomechanics unavailable ({type(e).__name__}: {e}) - continuing without")
        else:
            log("  biomechanics: [pose] extra not installed - skipping overlays "
                "(pip install -e '.[pose]')")

    moments: list[dict[str, Any]] = []
    for i, m in enumerate(selected):
        log(f"  moment {i+1}/{len(selected)}: {m['code']} at t={m['t_s']:.1f}s ...")
        assets = MomentAssets()
        rec: dict[str, Any] = dict(m)
        rec["index"] = i

        # 1) slow-motion error clip (always attempted)
        slow = slowmo_snippet(video, m["t_s"], mdir / f"moment_{i:02d}_slowmo.mp4",
                              smooth=smooth_slowmo)
        if slow:
            assets.slowmo = f"moments/{slow.name}"

        # 2) biomechanics window
        window = None
        if estimator is not None:
            try:
                frames, start_s, fps = extract_window_frames(
                    video, m["t_s"], mdir / f"moment_{i:02d}_frames")
                nominal = min(len(frames) - 1, max(0, round((m["t_s"] - start_s) * fps)))
                window = estimator.track_window(frames, m["player"], fps, start_s, nominal)
                rec["contact_t_s"] = round(window.contact_t_s, 2)
                rec["angles"] = window.angles
                from .pose import ankle_midpoint, draw_overlay, render_overlay_video
                contact_pose = window.poses[window.contact_idx]
                if contact_pose is not None:
                    mid = ankle_midpoint(contact_pose)
                    if mid:
                        rec["contact_px"] = [round(mid[0], 1), round(mid[1], 1)]
                        rec["contact_frame"] = str(
                            window.frame_paths[window.contact_idx].relative_to(out_dir))
                    ov = draw_overlay(window.frame_paths[window.contact_idx], contact_pose,
                                      window.angles, mdir / f"moment_{i:02d}_overlay.jpg")
                    assets.overlay = f"moments/{ov.name}"
                ovid = render_overlay_video(window.frame_paths, window.poses,
                                            mdir / f"moment_{i:02d}_overlay.mp4")
                if ovid:
                    assets.overlay_video = f"moments/{ovid.name}"
            except Exception as e:  # noqa: BLE001
                log(f"    pose failed for this moment ({type(e).__name__}: {e}) - continuing")
                window = None

        # 3) ghost comparison vs the player's own best same-type stroke
        if window is not None and estimator is not None:
            ref_t = find_reference_stroke(analyses, m["stroke"], m["player"], m["t_s"])
            if ref_t is not None:
                try:
                    ghost = _build_ghost(estimator, video, m, window, ref_t,
                                         mdir / f"moment_{i:02d}_ghost.mp4", mdir, i)
                    if ghost:
                        assets.ghost_video = f"moments/{ghost.name}"
                        rec["reference_t_s"] = ref_t
                except Exception as e:  # noqa: BLE001
                    log(f"    ghost overlay failed ({type(e).__name__}) - continuing")

        # 4) deep-dive coaching card
        if vlm is not None:
            frames_for_card = (window.frame_paths[::2][:8] if window is not None
                               else _card_frames(video, m["t_s"], mdir, i))
            if frames_for_card:
                card = _deepdive_card(vlm, m, frames_for_card, 3.0, rec.get("angles"))
                if card:
                    rec["card"] = card
                else:
                    log("    coaching card generation failed - moment keeps its assets")

        rec["assets"] = {k: v for k, v in vars(assets).items() if v}
        moments.append(rec)
        (mdir / f"moment_{i:02d}.json").write_text(json.dumps(rec, indent=2))
    return moments


def _card_frames(video: Path, t_s: float, mdir: Path, i: int) -> list[Path]:
    """Frames for the VLM card when pose is unavailable (no window extracted yet)."""
    try:
        frames, _, _ = extract_window_frames(video, t_s, mdir / f"moment_{i:02d}_frames",
                                             fps=4.0, max_side=784)
        return frames[:8]
    except Exception:  # noqa: BLE001
        return []


def _build_ghost(estimator, video: Path, moment: dict[str, Any], window,
                 ref_t: float, out_path: Path, mdir: Path, i: int):
    """Overlay the reference stroke's normalized skeleton onto the flagged window."""
    import numpy as np

    from .pose import normalize_to, render_overlay_video

    ref_frames, ref_start, ref_fps = extract_window_frames(
        video, ref_t, mdir / f"moment_{i:02d}_ref_frames")
    nominal = min(len(ref_frames) - 1, max(0, round((ref_t - ref_start) * ref_fps)))
    ref_win = estimator.track_window(ref_frames, moment["player"], ref_fps, ref_start, nominal)

    # align by contact frame, then map reference poses onto the subject's anchor
    n = len(window.poses)
    ghost_seq: list = [None] * n
    shift = ref_win.contact_idx - window.contact_idx
    for j in range(n):
        rj = j + shift
        if not (0 <= rj < len(ref_win.poses)):
            continue
        anchor, ref = window.poses[j], ref_win.poses[rj]
        if anchor is None or ref is None:
            continue
        mapped = normalize_to(anchor.kpts, ref.kpts)
        if mapped is not None:
            ghost_seq[j] = mapped
    if not any(g is not None for g in ghost_seq):
        return None
    return render_overlay_video(window.frame_paths, window.poses, out_path,
                                ghost_seq=ghost_seq)
