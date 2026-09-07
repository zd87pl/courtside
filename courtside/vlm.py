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
from typing import Any, Callable


@dataclass
class GenStats:
    load_s: float = 0.0
    wall_s: float = 0.0
    prompt_tokens: int | None = None
    generation_tokens: int | None = None
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
        on_token: Callable[[str], None] | None = None,
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
            # KV-cache quantization bits are an integer count (finding: --kv-bits
            # was passed through as a float).
            kwargs["kv_bits"] = int(self.kv_bits)

        # mlx-vlm's high-level generate() resets the tokenizer's stopping
        # criteria to the model's EOS ids before decoding; stream_generate does
        # not do this itself, so mirror it or the first token can stop decoding
        # (empty output).
        tok = getattr(self.processor, "tokenizer", self.processor)
        try:
            tok.stopping_criteria.reset(self.model.config.eos_token_id)
        except Exception:
            pass

        _reset_peak_memory()
        chunks: list[str] = []
        last = None
        t0 = time.perf_counter()
        for chunk in stream_generate(self.model, self.processor, formatted, **kwargs):
            chunks.append(chunk.text)
            if on_token is not None:
                on_token(chunk.text)
            last = chunk
        wall = time.perf_counter() - t0

        stats = GenStats(
            load_s=self.load_s,
            wall_s=wall,
            prompt_tokens=getattr(last, "prompt_tokens", None),
            generation_tokens=getattr(last, "generation_tokens", None),
            prompt_tps=getattr(last, "prompt_tps", None),
            generation_tps=getattr(last, "generation_tps", None),
            peak_gb=_peak_memory_gb(),
        )
        return "".join(chunks), stats

    def close(self) -> None:
        for attr in ("model", "processor"):
            if hasattr(self, attr):
                delattr(self, attr)
        gc.collect()
        try:
            import mlx.core as mx
            try:
                mx.clear_cache()
            except AttributeError:
                mx.metal.clear_cache()
        except Exception:
            pass


class ServerVLM:
    """OpenAI-compatible client (mlx_vlm.server, LM Studio, OpenRouter, ...).

    Local frames are sent as base64 data URLs - with a hosted endpoint like
    OpenRouter they leave the machine, so callers must label such runs as
    cloud, never on-device.
    """

    # class-level defaults so instances built without __init__ (tests, pickling)
    # still generate; __init__ sets the real per-instance values
    _schema_ok = True
    _reasoning_ok = False
    _disable_thinking = False

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 180.0):
        import os

        from openai import OpenAI  # optional dependency: pip install .[server]

        key = api_key or os.environ.get("OPENROUTER_API_KEY") \
            or os.environ.get("OPENAI_API_KEY") or "not-needed"
        self.client = OpenAI(base_url=base_url, api_key=key, timeout=timeout)
        self.model = model
        self.load_s = 0.0
        self._schema_ok = True  # flips off after a server rejects response_format
        # Qwen3.8 thinks by default; disable it to reserve the limited output
        # budget for analysis JSON. Other OpenRouter models retain low effort
        # with the existing compatibility fallback.
        self._reasoning_ok = "openrouter.ai" in base_url
        self._disable_thinking = self._reasoning_ok and model == "qwen/qwen3.8-27b"

    def ping(self) -> None:
        """Fail fast if the server is unreachable (finding: dead server hangs)."""
        self.client.models.list()

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
        on_token: Callable[[str], None] | None = None,  # accepted for parity; server path is non-streaming
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
        use_schema = json_schema is not None and self._schema_ok
        if use_schema:
            req["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "ClipAnalysis", "strict": True, "schema": json_schema},
            }
        if self._reasoning_ok:
            req["extra_body"] = {
                "reasoning": {"effort": "none" if self._disable_thinking else "low"},
            }

        def _create(r: dict) -> Any:
            try:
                return self.client.chat.completions.create(**r)
            except Exception as e:
                if getattr(e, "status_code", None) != 400:
                    raise
                # Progressive fallback on 400: some providers reject the
                # reasoning parameter, some reject response_format. Drop the
                # rejected extra, remember, retry - the Pydantic
                # validate-and-repair path is the backstop anyway.
                # Keep Qwen3.8's non-thinking mode even on schema fallback:
                # dropping it would silently restore the provider's thinking default.
                if "extra_body" in r and not self._disable_thinking:
                    self._reasoning_ok = False
                    r = dict(r)
                    r.pop("extra_body")
                    return _create(r)
                if "response_format" in r:
                    self._schema_ok = False
                    r = dict(r)
                    r.pop("response_format")
                    return self.client.chat.completions.create(**r)
                raise

        t0 = time.perf_counter()
        resp = _create(req)
        # One recovery retry for the two cut-short signatures: finish_reason
        # 'length' (a long rally clip outgrew the cap - the cap is a ceiling,
        # not a spend) and EMPTY content (a reasoning hybrid spent the whole
        # budget thinking). Both fail parse AND poison the repair round.
        choice = resp.choices[0]
        if (getattr(choice, "finish_reason", None) == "length"
                or not (choice.message.content or "").strip()) and max_tokens < 8000:
            req["max_tokens"] = min(8000, max_tokens * 2)
            resp = _create(req)
        wall = time.perf_counter() - t0
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        stats = GenStats(
            wall_s=wall,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            generation_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        )
        return text, stats

    def close(self) -> None:
        pass


# ---------- JSON extraction / repair ----------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_THINK_PAIR_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove reasoning blocks emitted by thinking models.

    Handles three shapes (finding: <think> leaks / unclosed tags break parsing):
      1. well-paired  <think>...</think>
      2. close-only   ...</think>REAL   (template pre-filled the opening tag)
      3. open-only    <think>REAL...    (truncated before closing - reasoning
                       is discarded; if nothing follows, the caller sees empty)
    """
    text = _THINK_PAIR_RE.sub("", text)
    if "</think>" in text:  # close-only: keep everything after the last close
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:  # open-only with no close: drop from the tag onward
        text = text.split("<think>", 1)[0]
    return text.strip()


def _first_json_object(s: str) -> dict | None:
    """Grab the first balanced {...} object, tolerating braces inside strings."""
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
    # unbalanced (truncated) - fall back to outermost slice
    end = s.rfind("}")
    if end > start:
        try:
            return json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def extract_json(text: str) -> dict:
    """Best-effort: strip fences / thinking blocks, grab outermost object.

    Tries the think-stripped text first, then the raw text (in case the model
    put its answer inside an unclosed <think> block, which stripping would
    otherwise discard), then any fenced block.
    """
    if not text or not text.strip():
        raise ValueError("model returned empty output")
    stripped = strip_think(text)
    for candidate in (stripped, text):
        m = _FENCE_RE.search(candidate)
        body = m.group(1) if m else candidate
        obj = _first_json_object(body)
        if isinstance(obj, dict):
            return obj
    raise ValueError("No JSON object found in model output")
