"""Frame extraction: ffmpeg does the 4K decode + downscale, not Python.

Per clip we target `fps` sampling but never exceed `max_frames` (the NUS
dissertation's sweet-spot finding: ~32 frames beats both sparse and
every-frame sampling for rally understanding). Frames are resized so the
long side is `max_side` px, which keeps vision-token counts predictable
(Qwen3-VL-class: roughly (side/28)^2 * AR tokens per frame).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .segment import Segment


class ToolMissingError(RuntimeError):
    """Raised when ffmpeg/ffprobe are not on PATH, with an actionable message."""


def require_tools() -> None:
    """Fail fast with a friendly message if ffmpeg/ffprobe are missing.

    Without this the first pipeline action is a raw FileNotFoundError deep in
    subprocess (finding: missing ffmpeg produces a raw FileNotFoundError).
    """
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise ToolMissingError(
            f"required tool(s) not found on PATH: {', '.join(missing)}. "
            "Install with `brew install ffmpeg` (macOS) or `apt install ffmpeg` (Linux)."
        )


@dataclass
class ClipFrames:
    segment: Segment
    fps_used: float
    frames: list[Path]  # ordered

    def timestamps(self) -> list[float]:
        """Absolute time (s) of each extracted frame in the full video."""
        return [self.segment.start_s + i / self.fps_used for i in range(len(self.frames))]

    def frame_for_time(self, t_s: float) -> Path | None:
        """The extracted frame nearest an absolute timestamp (for the report)."""
        if not self.frames:
            return None
        ts = self.timestamps()
        nearest = min(range(len(ts)), key=lambda i: abs(ts[i] - t_s))
        return self.frames[nearest]


def extract_clip_frames(
    video: Path,
    segment: Segment,
    out_dir: Path,
    fps: float = 4.0,
    max_frames: int = 32,
    max_side: int = 784,
) -> ClipFrames:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear any stale frames from a previous run: ffmpeg -y only overwrites
    # same-numbered files, and extraction globs the whole dir, so leftover
    # frames from a different fps/segment would silently contaminate this clip
    # (finding: stale frames from previous runs contaminate clips).
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()

    dur = max(0.1, segment.duration)
    fps_used = min(fps, max_frames / dur)

    # Long side -> max_side, preserve aspect ratio.
    vf = (
        f"fps={fps_used:.6f},"
        f"scale='if(gt(iw,ih),{max_side},-2)':'if(gt(iw,ih),-2,{max_side})':flags=lanczos"
    )
    pattern = out_dir / "frame_%04d.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{segment.start_s:.3f}", "-to", f"{segment.end_s:.3f}",
        "-i", str(video),
        "-vf", vf, "-q:v", "3",
        str(pattern),
    ]
    subprocess.run(cmd, check=True)

    frames = sorted(out_dir.glob("frame_*.jpg"))[:max_frames]
    if not frames:
        raise RuntimeError(f"No frames extracted for segment {segment.start_s:.1f}-{segment.end_s:.1f}s")
    return ClipFrames(segment=segment, fps_used=fps_used, frames=frames)


def extract_flag_snippet(
    video: Path,
    t_s: float,
    out_path: Path,
    pre: float = 1.5,
    post: float = 1.5,
    label: str | None = None,
    max_side: int = 640,
) -> Path | None:
    """Cut a short muted h264 loop around a flagged moment for the HTML report.

    Technique flaws are motion phenomena; a 3s loop reads better than a still in
    a pitch meeting (finding: flagged-moment video snippets). Best-effort - a
    failure returns None rather than aborting the run.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, t_s - pre)
    vf = f"scale='min({max_side},iw)':-2:flags=lanczos"
    if label:
        safe = label.replace(":", "\\:").replace("'", "")
        vf += (
            f",drawtext=text='{safe}':x=10:y=10:fontsize=20:fontcolor=white:"
            "box=1:boxcolor=black@0.5:boxborderw=6"
        )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{pre + post:.3f}",
        "-i", str(video),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-vf", vf, "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True)
        return out_path if out_path.exists() else None
    except (subprocess.CalledProcessError, OSError):
        return None


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    raw = out.stdout.strip()
    # Some containers report duration as "N/A" (finding: probe_duration crashes
    # on ffprobe N/A). Fall back to a stream-level probe, then to 0.0.
    try:
        return float(raw)
    except ValueError:
        pass
    out2 = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(out2.stdout.strip())
    except ValueError:
        return 0.0
