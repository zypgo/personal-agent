"""Resolve explicitly requested live-test credentials without copying them to disk."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def live_key(home: Path) -> str:
    """Honor injected credentials before the optional, operator-owned test-home file."""
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if not value:
        value = (dotenv_values(home / ".env").get("OPENAI_API_KEY") or "").strip()
    if not value:
        raise RuntimeError(
            "Live tests were requested, but OPENAI_API_KEY is missing. "
            f"Inject it into the environment or configure {home / '.env'}."
        )
    return value
