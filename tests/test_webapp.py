"""Web UI: session scanning, path safety, run manager, and view rendering."""

from __future__ import annotations

import json
import sys
import time

from courtside.report_html import _md_to_html
from courtside.webapp import AppState, RunManager, build_analysis_request, safe_child, scan_sessions
from courtside.webui import render_dashboard, render_run_page, render_session


def _make_session(tmp_path, name="match_courtside", video="match.mp4"):
    d = tmp_path / name
    (d / "clip_000").mkdir(parents=True)
    # stub frames: the views only need filenames on disk to build /frames URLs
    for i in (1, 2):
        (d / "clip_000" / f"frame_{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    doc = {
        "courtside_version": "0.1.0", "video": video, "video_duration_s": 120.0,
        "model": "test-model", "backend": "local", "on_device": True,
        "settings": {}, "created_at": "2026-07-19T00:00:00+00:00",
        "run_stats": {"total_tokens": 1000, "total_wall_s": 10.0, "video_duration_s": 120.0,
                      "realtime_factor": 12.0, "cloud_equiv_usd": 0.01, "on_device_usd": 0.0,
                      "bytes_uploaded": 0, "clips_ok": 1, "clips_failed": 0, "peak_gb": 9.1},
        "facts": {"total_strokes": 2, "clips_analyzed": 1,
                  "player_split": {"near": 1, "far": 1, "unknown": 0},
                  "top_technique_flags": {"late_preparation": 1}, "top_tactical_flags": {}},
        "clips": [{"index": 0, "frame_dir": "clip_000", "fps_used": 4.0, "status": "ok",
                   "analysis": {"start_s": 10.0, "end_s": 18.0, "confidence": "high",
                                "rally_summary": "test rally", "notes": "",
                                "strokes": [{"t_s": 11.0, "player": "near", "stroke": "forehand",
                                             "technique_flags": [{"code": "late_preparation",
                                                                  "severity": "high", "evidence": "e"}],
                                             "tactical_flags": []},
                                            {"t_s": 14.0, "player": "far", "stroke": "backhand",
                                             "technique_flags": [], "tactical_flags": []}]}}],
    }
    (d / "session.json").write_text(json.dumps(doc))
    return d


def test_scan_finds_sessions_and_legacy(tmp_path):
    _make_session(tmp_path)
    legacy = tmp_path / "old_courtside"
    legacy.mkdir()
    (legacy / "session.json").write_text(json.dumps([
        {"start_s": 0, "end_s": 5, "strokes": [], "rally_summary": "r",
         "confidence": "high", "notes": ""}]))
    found = scan_sessions([tmp_path])
    titles = sorted(r.title for r in found.values())
    assert titles == ["match.mp4", "old_courtside"]


def test_safe_child_blocks_traversal(tmp_path):
    base = tmp_path / "s"
    (base / "clip_000").mkdir(parents=True)
    (base / "clip_000" / "f.jpg").write_bytes(b"x")
    assert safe_child(base, "clip_000/f.jpg") is not None
    assert safe_child(base, "../secret") is None
    assert safe_child(base, "/etc/passwd") is None
    assert safe_child(base, "clip_000/../../s2") is None


def test_run_manager_completes_and_captures_log():
    rm = RunManager()
    run = rm.start([sys.executable, "-c", "print('hello'); print('world')"],
                   out_dir=__import__("pathlib").Path("/tmp"), video="v.mp4")
    for _ in range(100):
        if run.status != "running":
            break
        time.sleep(0.05)
    assert run.status == "done" and run.returncode == 0
    assert any("world" in l for l in run.tail())


def test_run_manager_rejects_concurrent():
    rm = RunManager()
    rm.start([sys.executable, "-c", "import time; time.sleep(2)"],
             out_dir=__import__("pathlib").Path("/tmp"), video="a.mp4")
    try:
        rm.start([sys.executable, "-c", "print(1)"],
                 out_dir=__import__("pathlib").Path("/tmp"), video="b.mp4")
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_views_render(tmp_path):
    _make_session(tmp_path)
    state = AppState([tmp_path])
    dash = render_dashboard(state)
    assert "New analysis" in dash and "match.mp4" in dash and "on-device" in dash

    ref = next(iter(state.sessions().values()))
    page = render_session(ref)
    assert "Session timeline" in page and "late_preparation" in page
    assert "processed on-device" in page and "Export report" in page
    # frames referenced by URL (served route), never embedded as data URIs
    assert f"/frames/{ref.sid}/clip_000/frame_0001.jpg" in page
    assert "data:image/jpeg" not in page

    class FakeRun:
        rid, video = "abc123", "v.mp4"
    assert "abc123" in render_run_page(FakeRun())


def test_dashboard_escapes_error():
    state = AppState([])
    out = render_dashboard(state, error="<script>alert(1)</script>")
    assert "<script>alert(1)" not in out and "&lt;script&gt;" in out


def test_run_page_escapes_video_name():
    class EvilRun:
        rid = "r1"
        video = 'x"><script>alert(1)</script>.mp4'
    out = render_run_page(EvilRun())
    assert "<script>alert(1)" not in out
    assert "&lt;script&gt;" in out


def test_run_manager_survives_non_utf8_output():
    rm = RunManager()
    run = rm.start([sys.executable, "-c",
                    "import sys; sys.stdout.buffer.write(b'ok\\n\\xff\\xfe bad bytes\\n')"],
                   out_dir=__import__("pathlib").Path("/tmp"), video="v.mp4")
    for _ in range(100):
        if run.status != "running":
            break
        time.sleep(0.05)
    # must terminate (not wedge forever) and keep the decodable content
    assert run.status == "done" and run.returncode == 0
    assert any("ok" in l for l in run.tail())
    # and a new run must be startable afterwards
    rm.start([sys.executable, "-c", "print(1)"],
             out_dir=__import__("pathlib").Path("/tmp"), video="w.mp4")


def test_malformed_session_json_is_skipped_not_fatal(tmp_path):
    _make_session(tmp_path)
    for i, bad in enumerate(["null", '"just a string"', "42"]):
        d = tmp_path / f"bad{i}_courtside"
        d.mkdir()
        (d / "session.json").write_text(bad)
    state = AppState([tmp_path])
    sessions = state.sessions()
    assert len(sessions) == 1  # only the valid one
    # dashboard renders despite the bad files on disk
    assert "match.mp4" in render_dashboard(state)


def test_mistyped_doc_fields_do_not_crash_views(tmp_path):
    d = _make_session(tmp_path)
    doc = json.loads((d / "session.json").read_text())
    doc["facts"]["total_strokes"] = "5"        # string where int expected
    doc["run_stats"]["realtime_factor"] = "fast"
    doc["run_stats"].pop("total_wall_s")       # partial run_stats
    (d / "session.json").write_text(json.dumps(doc))
    state = AppState([tmp_path])
    ref = next(iter(state.sessions().values()))
    assert "Session timeline" in render_session(ref)   # renders, no 500
    assert "match.mp4" in render_dashboard(state)


def test_analyze_argv_uses_end_of_options_separator(tmp_path):
    v = tmp_path / "-weird.mp4"
    v.write_bytes(b"x")
    argv, _, _ = build_analysis_request({"video": str(v)}, [tmp_path])
    sep = argv.index("--")
    assert argv[sep + 1] == str(v) and argv[-1] == str(v)


def test_form_field_whitelist():
    from courtside.webapp import ALLOWED_FORM_FIELDS
    assert "server_url" not in ALLOWED_FORM_FIELDS
    assert "download_dir" not in ALLOWED_FORM_FIELDS  # set internally, never from the form
    assert {"video", "model", "quick", "offline", "dry_run"} == ALLOWED_FORM_FIELDS


def test_md_renderer_joins_wrapped_lines():
    md = "## H\nfirst line\nsecond line\n\n- item one\n  wraps here\n- item two\n\n1. drill one\n   success line\n2. drill two\n"
    out = _md_to_html(md)
    assert "<p>first line second line</p>" in out
    assert "<li>item one wraps here</li>" in out
    assert out.count("<ol>") == 1 and "<li>drill one success line</li>" in out
    assert "<li>drill two</li>" in out
