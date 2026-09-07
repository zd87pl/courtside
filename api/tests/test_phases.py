"""Progress parsing, against the log lines the pipeline actually prints."""

from courtside_api.phases import Progress, derive

REAL_LOG = [
    "$ python -m courtside.analyze --out /w/out -- /w/source.mp4",
    "video: source.mp4  (40.3 min)",
    "segments: 146 active clips",
    "  dead time removed: 18.2 min of 40.3 min",
    "  clip 000    12.4-  31.9s  32 frames @ 4.00 fps",
    "  clip 001    44.1-  61.0s  32 frames @ 4.00 fps",
    "backend: server https://openrouter.ai/api/v1  model=qwen/qwen2.5-vl-72b-instruct",
    "[1/146] analyzing clip 12.4-31.9s ...",
    "    6 strokes, 2 flags, conf=high",
    "[73/146] analyzing clip 900.1-921.0s ...",
    "generating session report ...",
    "reports written: /w/out/session_report.md + report.html",
]


def test_phase_walks_the_pipeline_in_order():
    seen = []
    p = Progress()
    for line in REAL_LOG:
        p.feed(line)
        seen.append(p.phase)
    assert seen[0] == "queued"
    assert "segmenting" in seen and "extracting" in seen and "analyzing" in seen
    assert p.phase == "reporting"


def test_duration_and_clip_counts_are_extracted():
    p = Progress()
    for line in REAL_LOG:
        p.feed(line)
    assert p.video_duration_s == 40.3 * 60
    assert p.clips_total == 146
    assert p.clips_done == 146          # set to total once the report starts


def test_analyzing_progress_tracks_clip_index():
    p = Progress()
    for line in REAL_LOG[:8]:
        p.feed(line)
    assert p.phase == "analyzing" and p.pct == 40
    p.feed("[73/146] analyzing clip 900.1-921.0s ...")
    assert 60 < p.pct < 75


def test_progress_never_walks_backwards():
    p = Progress()
    for line in REAL_LOG:
        p.feed(line)
    high = p.pct
    p.feed("  clip 145   2400.0-2410.0s  32 frames @ 4.00 fps")   # a late stray line
    assert p.pct == high


def test_running_progress_is_capped_below_complete():
    phase, pct = derive(REAL_LOG, "running")
    assert phase == "reporting" and pct <= 98


def test_terminal_states_report_themselves():
    assert derive(REAL_LOG, "succeeded") == ("done", 100)
    assert derive(REAL_LOG, "failed")[0] == "failed"
    assert derive([], "cancelled") == ("cancelled", 4)
