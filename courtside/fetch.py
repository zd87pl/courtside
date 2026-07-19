"""Fetch remote footage (YouTube etc.) into the local workspace via yt-dlp.

The privacy story stays honest: fetching a URL obviously uses the network,
but the downloaded file is analyzed locally like any other video - nothing
about the footage or the analysis is uploaded. ``--offline`` therefore
refuses URL inputs outright.

yt-dlp is an optional extra (``pip install 'courtside[youtube]'``); a missing
install produces an actionable error, not a traceback.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable

URL_RE = re.compile(r"^https?://", re.IGNORECASE)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

# capped at 1080p mp4: plenty for 784px frame sampling, keeps downloads sane
_FORMAT = "mp4[height<=1080]/best[height<=1080]/best"


def is_url(s: str) -> bool:
    return bool(URL_RE.match(s.strip()))


def video_slug(url: str) -> str:
    """Stable short id for naming output dirs: the YouTube id when present."""
    u = url.strip()
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", u)
    if not m:
        m = re.search(r"(?:youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{6,})", u)
    if m:
        return m.group(1)
    return hashlib.md5(u.encode()).hexdigest()[:10]


def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def fetch_video(url: str, dest_dir: Path,
                on_line: Callable[[str], None] | None = None) -> Path:
    """Download ``url`` into ``dest_dir`` and return the local file path.

    Streams yt-dlp's progress lines to ``on_line`` so callers (CLI log, web
    run console) can show live download progress. Raises RuntimeError with an
    actionable message on any failure.
    """
    if not ytdlp_available():
        raise RuntimeError(
            "yt-dlp is not installed - install it with: pip install 'courtside[youtube]'"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".yt-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    argv = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--newline", "--restrict-filenames",
        "-f", _FORMAT, "--merge-output-format", "mp4",
        "-o", str(tmp / "%(title).80B-%(id)s.%(ext)s"),
        url,
    ]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip("\n"))
        rc = proc.wait()
    except OSError as e:
        raise RuntimeError(f"could not run yt-dlp: {e}") from e
    if rc != 0:
        _cleanup(tmp)
        raise RuntimeError(f"download failed (yt-dlp exit {rc}) for {url}")

    files = [f for f in tmp.iterdir() if f.suffix.lower() in VIDEO_EXTS]
    if not files:
        _cleanup(tmp)
        raise RuntimeError(f"yt-dlp finished but produced no video file for {url}")
    # largest file wins if a format left siblings behind
    src = max(files, key=lambda f: f.stat().st_size)
    final = dest_dir / src.name
    if final.exists():
        final.unlink()
    src.rename(final)
    _cleanup(tmp)
    return final


def _cleanup(tmp: Path) -> None:
    try:
        for f in tmp.iterdir():
            f.unlink()
        tmp.rmdir()
    except OSError:
        pass
