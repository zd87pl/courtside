"""VLM backends.

LocalVLM      - in-process mlx-vlm (load once, stream_generate per call).
ServerVLM     - any OpenAI-compatible endpoint (mlx_vlm.server, LM Studio).
                Uses strict json_schema constrained decoding when asked.

Both expose:  generate(prompt, images=[...], json_schema=None, ...) -> (text, stats)
"""

from __future__ import annotations

import base64
import gc
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GenStats:
    load_s: float = 0.0
    wall_s: float = 0.0
    prompt_tokens: int | None = None
    prompt_tps: float | None = None
    generation_tps: float | None = None
    peak_gb: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _peak_memory_gb() -> float | None:
    try:
        import mlx.core as mx
        try:
            return mx.get_peak_memory() / (1 << 30)
        except AttributeError:
            return mx.metal.get_peak_memory() / (1 << 30)
    except Exception:
        return None


def _reset_peak_memory() -> None:
    try:
        import mlx.core as mx
        try:
            mx.reset_peak_memory()
        except AttributeError:
            mx.metal.reset_peak_memory()
    except Exception:
        pass


class LocalVLM:
    def __init__(self, model_path: str, kv_bits: float | None = None):
        from mlx_vlm import load  # deferred: heavy import

        self.model_path = model_path
        self.kv_bits = kv_bits
        t0 = time.perf_counter()
        self.model, self.processor = load(model_path)
        self.load_s = time.perf_counter() - t0

    def generate(
        self,
        prompt: str,
        images: list[Path] | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        json_schema: dict | None = None,  # unused locally; kept for interface parity
    ) -> tuple[str, GenStats]:
        from mlx_vlm import stream_generate
        from mlx_vlm.prompt_utils import apply_chat_template

        images = images or []
        formatted = apply_chat_template(
            self.processor, self.model.config, prompt, num_images=len(images)
        )
        kwargs: dict[str, Any] = dict(max_tokens=max_tokens, temperature=temperature)
        if images:
            kwargs["image"] = [str(p) for p in images]
        if self.kv_bits:
            kwargs["kv_bits"] = self.kv_bits

        _reset_peak_memory()
        chunks: list[str] = []
        last = None
        t0 = time.perf_counter()
        for chunk in stream_generate(self.model, self.processor, formatted, **kwargs):
            chunks.append(chunk.text)
            last = chunk
        wall = time.perf_counter() - t0

        stats = GenStats(
            load_s=self.load_s,
            wall_s=wall,
            prompt_tokens=getattr(last, "prompt_tokens", None),
            prompt_tps=getattr(last, "prompt_tps", None),
            generation_tps=getattr(last, "generation_tps", None),
            peak_gb=_peak_memory_gb(),
        )
        return "".join(chunks), stats

    def close(self) -> None:
        del self.model, self.processor
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass


class ServerVLM:
    """OpenAI-compatible client. Local files are sent as base64 data URLs."""

    def __init__(self, base_url: str, model: str, api_key: str = "not-needed"):
        from openai import OpenAI  # optional dependency: pip install .[server]

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.load_s = 0.0

    @staticmethod
    def _data_url(path: Path) -> str:
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/jpeg;base64,{b64}"

    def generate(
        self,
        prompt: str,
        images: list[Path] | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        json_schema: dict | None = None,
    ) -> tuple[str, GenStats]:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for p in images or []:
            content.append({"type": "image_url", "image_url": {"url": self._data_url(p)}})

        req: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if json_schema is not None:
            req["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "ClipAnalysis", "strict": True, "schema": json_schema},
            }

        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(**req)
        wall = time.perf_counter() - t0
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        stats = GenStats(
            wall_s=wall,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
        )
        return text, stats

    def close(self) -> None:
        pass


# ---------- JSON extraction / repair ----------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Best-effort: strip fences / thinking blocks, grab outermost object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start : end + 1])
