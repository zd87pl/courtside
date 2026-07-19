"""Frame extraction: ffmpeg does the 4K decode + downscale, not Python.

Per clip we target `fps` sampling but never exceed `max_frames` (the NUS
dissertation's sweet-spot finding: ~32 frames beats both sparse and
every-frame sampling for rally understanding). Frames are resized so the
long side is `max_side` px, which keeps vision-token counts predictable
(Qwen3-VL-class: roughly (side/28)^2 * AR tokens per frame).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .segment import Segment


@dataclass
class ClipFrames:
    segment: Segment
    fps_used: float
    frames: list[Path]  # ordered

    def timestamps(self) -> list[float]:
        return [self.segment.start_s + i / self.fps_used for i in range(len(self.frames))]


def extract_clip_frames(
    video: Path,
    segment: Segment,
    out_dir: Path,
    fps: float = 4.0,
    max_frames: int = 32,
    max_side: int = 784,
) -> ClipFrames:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, segment.duration)
    fps_used = min(fps, max_frames / dur)

    # Long side -> max_side, preserve aspect ratio.
    vf = (
        f"fps={fps_used:.4f},"
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


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())
