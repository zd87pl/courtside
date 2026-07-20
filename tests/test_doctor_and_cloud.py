"""Doctor verdict logic, ServerVLM schema fallback, and the OpenRouter path."""

from __future__ import annotations

import pytest

from courtside import report
from courtside.doctor import _is_empty, _verdict
from courtside.webapp import DEFAULT_CLOUD_MODEL, OPENROUTER_URL, build_analysis_request


def _step(text="hello", ok=True, tokens=20, **kw):
    d = {"ok": ok, "text": text, "generation_tokens": tokens, "finish_reason": "stop"}
    d.update(kw)
    return d


ENV = {"mlx_vlm": "0.6.5"}


def test_verdict_instant_eos_points_at_regression():
    v = _verdict(ENV, {"img1": _step(text="", tokens=1)})
    joined = " ".join(v)
    assert "EOS" in joined and "0.6.3" in joined


def test_verdict_frame_count_ceiling():
    steps = {"img1": _step(), "img8": _step(), "img32": _step(text="", tokens=0)}
    joined = " ".join(_verdict(ENV, steps))
    assert "frame-count" in joined or "memory" in joined
    assert "--max-frames" in joined


def test_verdict_prompt_specific():
    steps = {"img1": _step(), "img8": _step(), "img32": _step(),
             "real": _step(text="")}
    joined = " ".join(_verdict(ENV, steps))
    assert "prompt" in joined.lower()


def test_verdict_healthy():
    steps = {"img1": _step(), "img8": _step(), "img32": _step(), "real": _step('{"x":1}')}
    joined = " ".join(_verdict(ENV, steps))
    assert "healthy" in joined


def test_is_empty():
    assert _is_empty({"ok": True, "text": "  "})
    assert not _is_empty({"ok": True, "text": "hi"})
    assert not _is_empty({"ok": False, "text": ""})  # raised, not empty


# ---------------- ServerVLM schema fallback ----------------

class _Err(Exception):
    status_code = 400


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **req):
        self.calls.append(req)
        if "response_format" in req:
            raise _Err("response_format not supported")

        class Msg:
            content = '{"ok": true}'

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]
            usage = None

        return Resp()


def test_servervlm_falls_back_when_schema_rejected(monkeypatch, tmp_path):
    from courtside.vlm import ServerVLM
    vlm = ServerVLM.__new__(ServerVLM)  # skip __init__ (no openai needed)
    fake = _FakeCompletions()

    class Client:
        class chat:
            completions = fake

    vlm.client = Client()
    vlm.model = "m"
    vlm._schema_ok = True

    text, _ = vlm.generate("p", images=None, json_schema={"type": "object"})
    assert text == '{"ok": true}'
    # first call had the schema (rejected), second didn't
    assert "response_format" in fake.calls[0] and "response_format" not in fake.calls[1]
    assert vlm._schema_ok is False
    # subsequent calls skip the schema entirely (no more 400 round-trips)
    vlm.generate("p2", images=None, json_schema={"type": "object"})
    assert "response_format" not in fake.calls[2]


# ---------------- OpenRouter form branch ----------------

def _video(tmp_path):
    v = tmp_path / "m.mp4"
    v.write_bytes(b"x")
    return v


def test_openrouter_requires_env_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_analysis_request({"video": str(_video(tmp_path)), "use_openrouter": "1"}, [tmp_path])


def test_openrouter_builds_server_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    argv, _, _ = build_analysis_request(
        {"video": str(_video(tmp_path)), "use_openrouter": "1", "cloud_model": "qwen/qwen3-vl-235b-a22b-instruct"},
        [tmp_path])
    assert "--server-url" in argv and OPENROUTER_URL in argv
    assert "qwen/qwen3-vl-235b-a22b-instruct" in argv
    # the key itself must never appear on the argv (visible in ps)
    assert not any("sk-or-test" in a for a in argv)


def test_openrouter_default_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    argv, _, _ = build_analysis_request(
        {"video": str(_video(tmp_path)), "use_openrouter": "1"}, [tmp_path])
    assert DEFAULT_CLOUD_MODEL in argv


def test_openrouter_conflicts_with_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_analysis_request(
            {"video": str(_video(tmp_path)), "use_openrouter": "1", "offline": "1"}, [tmp_path])


# ---------------- honest cost line for cloud runs ----------------

def test_cloud_run_stats_never_claim_zero_upload():
    rs = report.aggregate_run_stats(
        [{"wall_s": 10, "prompt_tokens": 10000, "generation_tokens": 300, "peak_gb": 0}],
        video_duration_s=120, segment_s=2, clips_ok=1, clips_failed=0,
        total_wall_s=30, on_device=False)
    assert rs["bytes_uploaded"] is None and rs["on_device_usd"] is None
    line = report.cost_summary_line(rs)
    assert "0 bytes uploaded" not in line and "on-device cost" not in line
    assert "frames uploaded" in line


def test_local_run_stats_keep_the_claim():
    rs = report.aggregate_run_stats(
        [{"wall_s": 10, "prompt_tokens": 10000, "generation_tokens": 300, "peak_gb": 9}],
        video_duration_s=120, segment_s=2, clips_ok=1, clips_failed=0, total_wall_s=30)
    assert "0 bytes uploaded" in report.cost_summary_line(rs)