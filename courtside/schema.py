"""Pydantic schemas: the structured contract between the VLM and the report.

These mirror the AceLens shared types (Rally -> Stroke -> Error) at demo
scale. `strict=True` json_schema from these models can be sent to any
OpenAI-compatible server (mlx_vlm.server, LM Studio) for constrained decoding.
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


def clip_json_schema() -> dict:
    """Strict JSON schema for constrained decoding on OpenAI-compatible servers."""
    return ClipAnalysis.model_json_schema()
