"""Default model selection must agree across runtime and deployment examples."""
from pathlib import Path
import tomllib

import pytest

from courtside_api.config import Settings, settings
from courtside_api.models import JobOptions

MODEL = "qwen/qwen3.8-27b"
URL = "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_model_configuration_uses_openrouter_qwen3(monkeypatch, value):
    for name in ("DEFAULT_MODEL", "OPENROUTER_URL"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    config = settings.__wrapped__()
    assert config.default_model == MODEL
    assert config.openrouter_url == URL
    argv = JobOptions().to_argv("/source.mp4", "/out", config.openrouter_url, config.default_model)
    assert argv[argv.index("--server-url") + 1] == URL
    assert argv[argv.index("--server-model") + 1] == MODEL
    assert "--model" not in argv  # never select the local MLX backend


def test_explicit_operator_and_job_overrides_are_preserved(monkeypatch):
    monkeypatch.setenv("DEFAULT_MODEL", "vendor/operator-vision-model")
    config = settings.__wrapped__()
    assert config.default_model == "vendor/operator-vision-model"
    argv = JobOptions(model="vendor/job-vision-model").to_argv(
        "/source.mp4", "/out", config.openrouter_url, config.default_model)
    assert argv[argv.index("--server-model") + 1] == "vendor/job-vision-model"


def test_fly_and_env_template_match_runtime_defaults():
    root = Path(__file__).resolve().parents[1]
    config = Settings(database_url="unused", admin_token="unused", openrouter_api_key="unused")
    fly = tomllib.loads((root / "fly.toml").read_text())
    example = dict(line.split("=", 1) for line in (root / ".env.example").read_text().splitlines()
                   if line and not line.startswith("#") and "=" in line)
    assert fly["env"]["DEFAULT_MODEL"] == example["DEFAULT_MODEL"] == config.default_model == MODEL
    assert example["OPENROUTER_URL"] == config.openrouter_url == URL
