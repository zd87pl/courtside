"""Schema round-trip, strict-mode compatibility, and flag normalization."""

from __future__ import annotations

from courtside.schema import (
    ClipAnalysis,
    Flag,
    Stroke,
    clip_json_schema,
    normalize_flag_code,
    strict_clip_json_schema,
)


def test_round_trip():
    a = ClipAnalysis(
        start_s=1.0, end_s=9.0, confidence="high", rally_summary="ok",
        strokes=[Stroke(t_s=3.0, player="near", stroke="forehand",
                        technique_flags=[Flag(code="late_preparation", severity="high", evidence="e")])],
    )
    b = ClipAnalysis.model_validate_json(a.model_dump_json())
    assert b == a


def _assert_strict(node, path="root"):
    if isinstance(node, dict):
        for bad in ("maxLength", "minLength", "default", "title"):
            assert bad not in node, f"{path}: strict-incompatible keyword {bad!r}"
        if node.get("type") == "object" and "properties" in node:
            props = set(node["properties"])
            assert set(node.get("required", [])) == props, f"{path}: required != properties"
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
        for k, v in node.items():
            _assert_strict(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _assert_strict(v, f"{path}[{i}]")


def test_strict_schema_is_openai_compatible():
    _assert_strict(strict_clip_json_schema())


def test_plain_schema_still_generated():
    s = clip_json_schema()
    assert s["type"] == "object" and "strokes" in s["properties"]


def test_flag_normalization():
    assert normalize_flag_code("late_prep") == "late_preparation"
    assert normalize_flag_code("Late Preparation") == "late_preparation"
    assert normalize_flag_code("missing_split_step") == "no_split_step"
    assert normalize_flag_code("some_novel_code") == "some_novel_code"
