"""URL detection, slug extraction, and the web app's URL analyze branch."""

from __future__ import annotations

from pathlib import Path

import pytest

from courtside.fetch import is_url, video_slug
from courtside.webapp import build_analysis_request


def test_is_url():
    assert is_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert is_url("http://youtu.be/abc123xyz")
    assert is_url("  https://example.com/v.mp4")
    assert not is_url("/home/me/match.mp4")
    assert not is_url("match.mp4")
    assert not is_url("ftp://host/x.mp4")


def test_video_slug_variants():
    assert video_slug("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_slug("https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"
    assert video_slug("https://youtube.com/shorts/AbC12345678") == "AbC12345678"
    assert video_slug("https://www.youtube.com/watch?list=PL1&v=xYz98765432") == "xYz98765432"
    # non-YouTube URLs get a stable hash
    a = video_slug("https://example.com/some/video")
    assert a == video_slug("https://example.com/some/video") and len(a) == 10


def test_build_request_local_file(tmp_path):
    v = tmp_path / "m.mp4"
    v.write_bytes(b"x")
    argv, out_dir, label = build_analysis_request({"video": str(v), "quick": "1"}, [tmp_path])
    assert str(v) in argv and "--max-clips" in argv
    assert out_dir == tmp_path / "m_courtside" and label == "m.mp4"


def test_build_request_rejects_missing_and_nonvideo(tmp_path):
    with pytest.raises(ValueError):
        build_analysis_request({"video": str(tmp_path / "nope.mp4")}, [tmp_path])
    t = tmp_path / "x.txt"
    t.write_text("hi")
    with pytest.raises(ValueError):
        build_analysis_request({"video": str(t)}, [tmp_path])
    with pytest.raises(ValueError):
        build_analysis_request({"video": ""}, [tmp_path])


def test_build_request_url(tmp_path, monkeypatch):
    monkeypatch.setattr("courtside.webapp.ytdlp_available", lambda: True)
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    argv, out_dir, label = build_analysis_request({"video": url, "dry_run": "1"}, [tmp_path])
    assert url in argv and "--download-dir" in argv and str(tmp_path) in argv
    assert out_dir == tmp_path / "yt_dQw4w9WgXcQ_courtside"
    assert label == url


def test_build_request_url_conflicts(tmp_path, monkeypatch):
    url = "https://youtu.be/dQw4w9WgXcQ"
    # offline + URL is a contradiction
    monkeypatch.setattr("courtside.webapp.ytdlp_available", lambda: True)
    with pytest.raises(ValueError, match="[Oo]ffline"):
        build_analysis_request({"video": url, "offline": "1"}, [tmp_path])
    # missing yt-dlp yields an actionable message
    monkeypatch.setattr("courtside.webapp.ytdlp_available", lambda: False)
    with pytest.raises(ValueError, match="yt-dlp"):
        build_analysis_request({"video": url}, [tmp_path])
