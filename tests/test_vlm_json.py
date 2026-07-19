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


def test_unclosed_think_raises():
    # truncated mid-thought: no real answer -> must raise so the caller repairs,
    # rather than silently parsing JSON found inside the reasoning.
    with pytest.raises(ValueError):
        extract_json('<think>thinking {"fake": 1} and cut off')


def test_no_object_raises():
    with pytest.raises(ValueError):
        extract_json("there is no json here")


def test_strip_think_variants():
    assert strip_think("<think>a</think>B") == "B"
    assert strip_think("a</think>B") == "B"
    assert strip_think("<think>a") == ""
