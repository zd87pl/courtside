import sys

import pytest
from pydantic import ValidationError

from courtside_api.models import JobOptions, StartJobRequest


def argv(**kw):
    return JobOptions(**kw).to_argv("/w/source.mp4", "/w/out",
                                    "https://openrouter.ai/api/v1", "vendor/default")


def test_defaults_render_a_server_backed_invocation():
    a = argv()
    assert a[:3] == [sys.executable, "-m", "courtside.analyze"]
    assert "--server-url" in a and "https://openrouter.ai/api/v1" in a
    assert a[a.index("--server-model") + 1] == "vendor/default"
    # No --api-key: the key reaches the child through the environment, never
    # through argv where `ps` would show it.
    assert "--api-key" not in a


def test_video_path_is_positional_after_the_option_terminator():
    a = argv()
    assert a[-2:] == ["--", "/w/source.mp4"]


def test_max_clips_zero_means_the_whole_match():
    assert "--max-clips" not in argv(max_clips=0)
    assert argv(max_clips=3)[argv(max_clips=3).index("--max-clips") + 1] == "3"


def test_pose_and_heatmap_map_to_their_flags():
    assert "--no-pose" in argv(pose=False)
    assert "--no-pose" not in argv(pose=True)
    assert "--heatmap" in argv(heatmap=True)
    assert "--heatmap" not in argv()


def test_window_is_only_sent_for_fixed_segmentation():
    assert "--window" not in argv(segment="auto", window=15)
    assert "--window" in argv(segment="fixed", window=15)


@pytest.mark.parametrize("bad", [
    "vendor/model; rm -rf /",      # shell metacharacters
    "--server-url",                 # a flag masquerading as a model name
    "vendor/model\nqwen",          # newline injection
    "x" * 200,                      # unbounded length
])
def test_model_names_that_could_confuse_the_child_are_rejected(bad):
    with pytest.raises(ValidationError):
        JobOptions(model=bad)


@pytest.mark.parametrize("bad", ["--from-ts", "1:2:3:4", "12; ls", "abc"])
def test_timestamps_are_validated(bad):
    with pytest.raises(ValidationError):
        JobOptions(from_ts=bad)


@pytest.mark.parametrize("good", ["90", "12:30", "1:02:03", "12:30.5"])
def test_reasonable_timestamps_pass(good):
    assert JobOptions(from_ts=good).from_ts == good


def test_unknown_options_are_rejected_rather_than_silently_dropped():
    with pytest.raises(ValidationError):
        JobOptions(gpu=True)


def test_webhook_must_be_https():
    with pytest.raises(ValidationError):
        StartJobRequest(webhook_url="http://example.com/hook")
    assert StartJobRequest(webhook_url="https://example.com/hook").webhook_url


def test_out_of_range_knobs_are_clamped_by_validation():
    with pytest.raises(ValidationError):
        JobOptions(max_frames=1000)
    with pytest.raises(ValidationError):
        JobOptions(fps=0)
