"""Pydantic schemas: the structured contract between the VLM and the report.

These mirror the AceLens shared types (Rally -> Stroke -> Error) at demo
scale. A strict-mode-compatible JSON schema (see ``strict_clip_json_schema``)
can be sent to any OpenAI-compatible server for constrained decoding.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

StrokeType = Literal[
    "serve", "forehand", "backhand", "forehand_volley", "backhand_volley",
    "overhead", "slice", "drop_shot", "lob", "return", "unknown",
]
Player = Literal["near", "far", "unknown"]
Severity = Literal["low", "medium", "high"]
Confidence = Literal["low", "medium", "high"]

# Canonical flag vocabulary the prompt already names. Free-form codes fragment
# report aggregation across synonyms (late_preparation vs late_prep), so the
# report layer normalizes toward these (finding: Flag.code is free-form).
CANONICAL_TECHNIQUE_CODES = {
    "late_preparation", "early_preparation", "contact_point_late",
    "contact_point_low", "swing_path_steep", "swing_path_flat",
    "poor_balance", "poor_recovery", "short_follow_through", "low_ball_toss",
    "high_ball_toss", "inconsistent_toss", "no_split_step", "footwork_spacing",
    "open_stance_forced", "closed_stance_forced",
}
CANONICAL_TACTICAL_CODES = {
    "short_ball_no_approach", "backhand_corner_camped", "no_depth_variation",
    "serve_placement_predictable", "passive_mid_rally", "poor_recovery_position",
    "no_net_approach", "over_hitting", "no_pace_variation",
}
CANONICAL_FLAG_CODES = CANONICAL_TECHNIQUE_CODES | CANONICAL_TACTICAL_CODES


def normalize_flag_code(code: str) -> str:
    """Collapse obvious synonyms to a canonical code for aggregation."""
    c = code.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "late_prep": "late_preparation",
        "preparation_late": "late_preparation",
        "prep_late": "late_preparation",
        "no_splitstep": "no_split_step",
        "split_step_missing": "no_split_step",
        "missing_split_step": "no_split_step",
        "low_toss": "low_ball_toss",
        "toss_low": "low_ball_toss",
        "predictable_serve": "serve_placement_predictable",
    }
    return aliases.get(c, c)


class Flag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="snake_case identifier, e.g. late_preparation, low_ball_toss, short_ball_no_approach")
    severity: Severity
    evidence: str = Field(max_length=300, description="What is visible in the frames that supports this flag")


class Stroke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_s: float = Field(description="Approximate time of contact in seconds from the start of the FULL video")
    player: Player
    stroke: StrokeType
    technique_flags: list[Flag] = Field(default_factory=list)
    tactical_flags: list[Flag] = Field(default_factory=list)


class ClipAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float
    end_s: float
    strokes: list[Stroke] = Field(default_factory=list)
    rally_summary: str = Field(max_length=500)
    confidence: Confidence
    notes: str = Field(default="", max_length=300)


class Drill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=80)
    setup: str = Field(max_length=300)
    success_criterion: str = Field(max_length=200)


class CoachingCard(BaseModel):
    """Deep-dive analysis of one flagged moment - the 'show them what to fix' unit."""

    model_config = ConfigDict(extra="forbid")

    what_happened: str = Field(max_length=450, description="What the player did on this stroke, concretely")
    why_it_matters: str = Field(max_length=350, description="The cost in points/consistency")
    correction: str = Field(max_length=450, description="The specific change to make next time")
    target: str = Field(max_length=200, description="A measurable cue, e.g. an angle range or timing cue")
    drill: Drill
    confidence: Confidence


def coaching_card_schema() -> dict:
    return CoachingCard.model_json_schema()


def clip_json_schema() -> dict:
    """Plain JSON schema (used verbatim inside the clip prompt)."""
    return ClipAnalysis.model_json_schema()


def _make_strict(node: dict) -> dict:
    """Recursively coerce a Pydantic JSON schema into OpenAI strict-mode form.

    Strict mode requires every property to appear in ``required`` and rejects
    ``maxLength``/``default``/``title`` and similar annotations. Pydantic omits
    defaulted fields from ``required`` and emits ``maxLength``/``default``, so
    the raw schema is silently non-compliant (finding: strict:true schema is not
    strict-mode compliant). This normalizes it.
    """
    if not isinstance(node, dict):
        return node
    out: dict = {}
    for k, v in node.items():
        if k in ("maxLength", "minLength", "default", "title"):
            continue
        if isinstance(v, dict):
            out[k] = _make_strict(v)
        elif isinstance(v, list):
            out[k] = [_make_strict(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"].keys())
        out["additionalProperties"] = False
    return out


def strict_clip_json_schema() -> dict:
    """Strict-mode-compatible schema for OpenAI-compatible constrained decoding."""
    return _make_strict(ClipAnalysis.model_json_schema())
