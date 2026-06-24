#!/usr/bin/env python3
"""Validate every fixture against its message schema.

Each fixture's top-level "type" selects schema/<type>.json. Run in both
consumers' CI so any schema/fixture drift fails the build.

    pip install jsonschema
    python validate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("missing dependency: pip install jsonschema")

ROOT = Path(__file__).parent
SCHEMA_DIR = ROOT / "schema"
FIXTURE_DIR = ROOT / "fixtures"


def load(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def main() -> int:
    expected_version = int((ROOT / "VERSION").read_text().strip())
    schemas = {p.stem: load(p) for p in SCHEMA_DIR.glob("*.json")}
    for s in schemas.values():
        Draft202012Validator.check_schema(s)

    failures = 0
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    if not fixtures:
        return 0

    for fx in fixtures:
        msg = load(fx)
        mtype = msg.get("type")
        schema = schemas.get(mtype)
        if schema is None:
            print(f"FAIL {fx.name}: no schema for type={mtype!r}")
            failures += 1
            continue
        errors = sorted(Draft202012Validator(schema).iter_errors(msg), key=lambda e: e.path)
        if errors:
            failures += 1
            print(f"FAIL {fx.name} ({mtype}):")
            for e in errors:
                loc = "/".join(map(str, e.path)) or "<root>"
                print(f"   - {loc}: {e.message}")
            continue
        # extra guard: version fields must match VERSION
        if "protocol_version" in msg and msg["protocol_version"] != expected_version:
            failures += 1
            print(f"FAIL {fx.name}: protocol_version {msg['protocol_version']} != VERSION {expected_version}")
            continue
        print(f"ok   {fx.name} ({mtype})")

    print(f"\n{len(fixtures) - failures}/{len(fixtures)} fixtures valid (protocol v{expected_version})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
