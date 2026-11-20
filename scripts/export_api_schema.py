#!/usr/bin/env python3
"""Export the static V3 OpenAPI contract without opening application data.

uv run python scripts/export_api_schema.py
uv run python scripts/export_api_schema.py --output /tmp/personal-agent-openapi.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from personal_agent.channels.web.openapi import document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("frontend/generated/openapi.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote API contract to {args.output}")


if __name__ == "__main__":
    main()
