"""HTML report renders from structured data alone (the instant-replay path)."""

from __future__ import annotations

from courtside import report
from courtside.report_html import render_html
from courtside.schema import ClipAnalysis, Flag, Stroke


def _doc():
    a = ClipAnalysis(
        start_s=10, end_s=18, confidence="high", rally_summary="Baseline rally.",
        strokes=[Stroke(t_s=11, player="near", stroke="forehand",
                        technique_flags=[Flag(code="late_preparation", severity="high", evidence="racquet back")])],
    )
    facts = report.compute_session_facts([a])
    rs = report.aggregate_run_stats(
        [{"wall_s": 10, "prompt_tokens": 12000, "generation_tokens": 200, "peak_gb": 40.0}],
        video_duration_s=120.0, segment_s=2.0, clips_ok=1, clips_failed=1)
    return report.build_session_doc(
        version="0.1.0", video_name="m.mp4", video_duration_s=120.0, model="qwen",
        backend="local", settings={}, created_at="t", run_stats=rs, facts=facts,
        clips=[
            {"index": 0, "frame_dir": "clip_000", "fps_used": 4.0, "status": "ok", "analysis": a.model_dump()},
            {"index": 1, "frame_dir": "clip_001", "fps_used": 4.0, "status": "failed",
             "error": "boom", "analysis": None},
        ],
    )


def test_render_is_self_contained_and_survives_missing_frames(tmp_path):
    html = render_html(tmp_path, _doc())  # no frame files on disk
    assert "<svg" in html                      # timeline renders
    assert "on-device" in html                 # privacy badge
    assert "late_preparation" in html          # flagged moment
    assert "Processed 2.0 min" in html         # cost line
    assert "skipped" in html                   # failed clip surfaced, not crashed
    assert "http://" not in html and "https://" not in html  # no external refs


def test_render_embeds_frames_as_data_uris(tmp_path):
    import subprocess, shutil
    if shutil.which("ffmpeg") is None:
        return  # frame embedding is exercised by the pipeline tests when ffmpeg exists
    fd = tmp_path / "clip_000"
    fd.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=160x90:rate=4", "-frames:v", "4",
                    str(fd / "frame_%04d.jpg")], check=True)
    html = render_html(tmp_path, _doc())
    assert "data:image/jpeg;base64," in html
