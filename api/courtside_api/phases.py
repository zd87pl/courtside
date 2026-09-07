"""Turn the CLI's stdout into a coarse phase + percentage.

Adapted from `courtside.webapp.derive_progress`, but decoupled from its `Run`
dataclass so the API package does not import the local UI. The phase names are
the ones the pipeline already prints, and are the ones the mobile client shows:

    queued -> downloading -> segmenting -> extracting -> analyzing -> reporting -> done
"""

from __future__ import annotations

import re

PHASES = ("queued", "downloading", "segmenting", "extracting",
          "analyzing", "reporting", "done")

_ANALYZE_RE = re.compile(r"\[(\d+)/(\d+)\]\s+analyzing")
_CLIP_RE = re.compile(r"clip \d+\s")
_SEGMENTS_RE = re.compile(r"^segments:\s*(\d+)")
_DURATION_RE = re.compile(r"^video:\s+.*\(([\d.]+)\s*min\)")


class Progress:
    """Rolling parse of the child's log. Fed one line at a time by the worker."""

    def __init__(self) -> None:
        self.phase = "queued"
        self.pct = 4
        self.clips_total: int | None = None
        self.clips_done: int | None = None
        self.video_duration_s: float | None = None

    def feed(self, line: str) -> None:
        s = line.strip()
        if not s:
            return

        m = _DURATION_RE.match(s)
        if m:
            self.video_duration_s = float(m.group(1)) * 60.0

        if s.startswith("fetching") or s.startswith("[download]") or s.startswith("[youtube]"):
            self._advance("downloading", 10)
        elif s.startswith("segments:") or "auto-segmentation" in s:
            self._advance("segmenting", 24)
            m = _SEGMENTS_RE.match(s)
            if m:
                self.clips_total = int(m.group(1))
        elif _CLIP_RE.match(s):
            self._advance("extracting", 34)
        elif "analyzing clip" in s:
            m = _ANALYZE_RE.search(s)
            if m:
                i, n = int(m.group(1)), max(int(m.group(2)), 1)
                self.clips_total, self.clips_done = n, i - 1
                self._advance("analyzing", 40 + int(50 * (i - 1) / n))
        elif "generating session report" in s:
            if self.clips_total is not None:
                self.clips_done = self.clips_total
            self._advance("reporting", 93)

    def _advance(self, phase: str, pct: int) -> None:
        # Monotonic: a late stray line must never walk the bar backwards.
        if pct >= self.pct:
            self.phase, self.pct = phase, pct

    def snapshot(self, status: str) -> tuple[str, int]:
        if status == "succeeded":
            return "done", 100
        if status in ("failed", "cancelled"):
            return status, self.pct
        return self.phase, min(self.pct, 98)


def derive(lines: list[str], status: str = "running") -> tuple[str, int]:
    """One-shot convenience over Progress (used by tests and backfills)."""
    p = Progress()
    for line in lines:
        p.feed(line)
    return p.snapshot(status)
