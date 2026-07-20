"""JSON extraction and thinking-model output handling."""

from __future__ import annotations

import pytest

from courtside.vlm import extract_json, strip_think


def test_plain_object():
    assert extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_fenced():
    assert extract_json("```json\n{\"x\": 2}\n```") == {"x": 2}


def test_brace_inside_string():
    assert extract_json('prefix {"a": "has } brace", "b": 1} suffix')["a"] == "has } brace"


def test_paired_think_stripped():
    assert extract_json('<think>noise {"fake": 1}</think>{"y": 3}') == {"y": 3}


def test_close_only_think():
    # template pre-filled the opening tag; only the close survives
    assert extract_json('reasoning here</think>{"z": 4}') == {"z": 4}


def test_unclosed_think_recovers_json():
    # a model that put its answer inside an unclosed <think> block: recover the
    # object rather than discard it (better than a hard failure on real output).
    assert extract_json('<think>here is the answer {"y": 3}') == {"y": 3}


def test_empty_output_raises():
    with pytest.raises(ValueError, match="empty"):
        extract_json("   ")


def test_no_object_raises():
    with pytest.raises(ValueError):
        extract_json("there is no json here")


def test_balanced_braces_with_trailing_prose():
    # extra prose after a complete object must not break parsing
    assert extract_json('{"a": 1} and then the model kept talking } ] nonsense')["a"] == 1


def test_strip_think_variants():
    assert strip_think("<think>a</think>B") == "B"
    assert strip_think("a</think>B") == "B"
    assert strip_think("<think>a") == ""
