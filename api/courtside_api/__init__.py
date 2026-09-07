"""Courtside Cloud API - async job service around the courtside pipeline.

Standalone: its own Postgres, its own API-key auth, its own object storage.
It shells out to the `courtside` CLI with an OpenAI-compatible backend
(OpenRouter), so no GPU and no Apple Silicon are required.
"""

__version__ = "0.1.0"
