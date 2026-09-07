#!/usr/bin/env python3
"""Export client-generation schema without starting services or using real keys."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("ADMIN_TOKEN", "schema-export-only")
from courtside_api.app import app

print(json.dumps(app.openapi(), indent=2))
