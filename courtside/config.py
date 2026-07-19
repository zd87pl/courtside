"""Model registry and defaults, tuned for a MacBook Pro M5 Max / 128GB.

Approximate weight sizes are for the quantized checkpoints only; add KV cache
and vision activations on top (a 32-frame clip at max_side=784 is roughly
12-16k vision tokens for Qwen3-VL-class models).

Repo IDs follow mlx-community naming conventions. If a listed quant is
missing on the Hub, either pick another bit-width from the same family or
convert locally:  python -m mlx_vlm.convert --hf-path <org/model> -q --q-bits 8
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repo: str
    approx_weights_gb: float
    notes: str


MODELS: dict[str, ModelSpec] = {
    "qwen3-vl-32b": ModelSpec(
        key="qwen3-vl-32b",
        repo="mlx-community/Qwen3-VL-32B-Instruct-8bit",
        approx_weights_gb=35,
        notes="Default. Dense 32B, strong video + instruction following, big headroom on 128GB.",
    ),
    "qwen3-vl-32b-thinking": ModelSpec(
        key="qwen3-vl-32b-thinking",
        repo="mlx-community/Qwen3-VL-32B-Thinking-8bit",
        approx_weights_gb=35,
        notes="Reasoning variant; use --enable-thinking ideas via prompt, slower but better on tactics.",
    ),
    "qwen3-vl-30b-a3b": ModelSpec(
        key="qwen3-vl-30b-a3b",
        repo="mlx-community/Qwen3-VL-30B-A3B-Instruct-8bit",
        approx_weights_gb=32,
        notes="MoE (3B active): fastest decode of the big options, great for iteration.",
    ),
    "qwen3-vl-8b": ModelSpec(
        key="qwen3-vl-8b",
        repo="mlx-community/Qwen3-VL-8B-Instruct-8bit",
        approx_weights_gb=9,
        notes="Smoke tests. Per the Qwen3-VL tech report, ~Qwen2.5-VL-72B-level on video tasks.",
    ),
    "glm-4.6v-flash": ModelSpec(
        key="glm-4.6v-flash",
        repo="lmstudio-community/GLM-4.6V-Flash-MLX-8bit",
        approx_weights_gb=10,
        notes="Z.ai 9B flash VLM; useful second opinion / architecture comparison.",
    ),
    "qwen3-vl-235b": ModelSpec(
        key="qwen3-vl-235b",
        repo="mlx-community/Qwen3-VL-235B-A22B-Instruct-3bit",
        approx_weights_gb=97,
        notes=(
            "Stretch flagship (MoE, 22B active). Needs the iogpu wired-limit bump from the "
            "README, --kv-bits 4, and modest frame counts. 4-bit does NOT fit in 128GB."
        ),
    ),
}

DEFAULT_MODEL_KEY = "qwen3-vl-32b"

# Frame sampling defaults - informed by the NUS dissertation finding that
# ~32 frames beat both 8 frames and every-frame sampling for rally tasks.
DEFAULT_FPS = 4.0
DEFAULT_MAX_FRAMES = 32
DEFAULT_MAX_SIDE = 784  # long-side pixels per frame sent to the VLM

# Segmentation defaults (motion-energy detector in segment.py)
DEFAULT_MIN_RALLY_S = 2.5
DEFAULT_MAX_CLIP_S = 45.0


def resolve_model(key_or_repo: str) -> str:
    """Accept either a registry key or a raw HF repo id / local path."""
    spec = MODELS.get(key_or_repo)
    return spec.repo if spec else key_or_repo
