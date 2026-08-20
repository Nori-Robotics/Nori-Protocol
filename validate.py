#!/usr/bin/env python3
"""Validate every fixture against its message schema, and the schemas against
this repo's own rules.

Each fixture's top-level "type" selects the schema of the same name, in the
matching subdirectory: fixtures/session/record_status.json is checked against
schema/session/record_status.json. A flat layout (no subdirectories) works too
-- fixtures/ack.json against schema/ack.json.

Run in every consumer's CI so any schema/fixture drift fails the build.

    pip install jsonschema
    python validate.py

WHAT THIS CAN AND CANNOT CATCH. It proves the schemas and fixtures agree with
each other and with the repo's rules. It CANNOT prove an implementation matches
either -- a field a daemon emits but nobody wrote down is invisible here, which
is exactly how `servo_temps` drifted for three weeks. Catching that needs each
consumer to feed real output through these schemas in its own tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("missing dependency: pip install jsonschema")

try:  # jsonschema >= 4.18 resolves cross-file $ref through a registry
    from referencing import Registry, Resource

    HAVE_REGISTRY = True
except ImportError:  # pragma: no cover - older jsonschema
    HAVE_REGISTRY = False

ROOT = Path(__file__).parent
SCHEMA_DIR = ROOT / "schema"
FIXTURE_DIR = ROOT / "fixtures"

# A message schema must close itself against unknown fields, so drift fails here
# rather than being silently accepted.
STRICTNESS_KEYS = ("additionalProperties", "unevaluatedProperties")

# Schemas whose filename starts with "_" are reusable FRAGMENTS, not message
# types: no fixture describes them and they must stay OPEN so extenders can add
# fields. They are still parsed and checked as valid schemas.
#
# This split is forced by how JSON Schema composition actually works, which is
# worth stating because two plausible-looking layouts silently fail:
#   * `additionalProperties: false` on a base rejects an extender's new fields --
#     it cannot see properties declared outside its own subschema.
#   * `unevaluatedProperties: false` on a base is no better: inside a $ref it is
#     evaluated in its own scope, so it rejects the extender's fields too, and
#     when the base fails its annotations are dropped, cascading into "nothing
#     was evaluated" at the extender.
# The working shape is: an OPEN `_core` fragment holding the shared properties,
# and each concrete message schema doing
#     allOf: [{$ref: _core}], properties: {...extras}, unevaluatedProperties: false
# which keeps every message schema strict while still sharing one definition.
FRAGMENT_PREFIX = "_"


def is_fragment(key: str) -> bool:
    return Path(key).name.startswith(FRAGMENT_PREFIX)


def load(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def rel_key(path: Path, root: Path) -> str:
    """'ack' for <root>/ack.json, 'session/ack' for <root>/session/ack.json."""
    return path.relative_to(root).with_suffix("").as_posix()


def build_registry(schemas: dict[str, dict]):
    """Let schemas $ref each other by $id. Returns None when `referencing` is
    unavailable, in which case cross-file $ref is unsupported and reported."""
    if not HAVE_REGISTRY:
        return None
    registry = Registry()
    for schema in schemas.values():
        if "$id" in schema:
            registry = registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
    return registry


def validator_for(schema: dict, registry) -> Draft202012Validator:
    if registry is None:
        return Draft202012Validator(schema)
    return Draft202012Validator(schema, registry=registry)


def check_schema_rules(schemas: dict[str, dict], fixtures: list[Path]) -> int:
    """Repo rules that are not expressible in JSON Schema itself."""
    failures = 0

    # 1. Every schema is exercised by at least one fixture. An unexercised
    #    schema is an untested one -- it can say anything and nobody notices.
    covered = set()
    for fx in fixtures:
        msg = load(fx)
        mtype = msg.get("type")
        if isinstance(mtype, str):
            subdir = fx.parent.relative_to(FIXTURE_DIR).as_posix()
            covered.add(f"{subdir}/{mtype}" if subdir != "." else mtype)
    for key in sorted(schemas):
        if is_fragment(key):
            continue  # fragments are exercised through the schemas that $ref them
        if key not in covered:
            print(f"FAIL schema/{key}.json: no fixture exercises it")
            failures += 1

    # 2. Fragments must carry $id: build_registry keys on it, so a fragment without one is
    #    unresolvable and every $ref to it raises mid-validation.
    for key, schema in sorted(schemas.items()):
        if is_fragment(key) and "$id" not in schema:
            print(
                f"FAIL schema/{key}.json: a fragment must declare \"$id\" -- $ref is "
                "resolved through a registry keyed on it, and without one every schema "
                "that extends this fragment fails to resolve at validation time"
            )
            failures += 1

    # 3. Every message schema closes itself (see STRICTNESS_KEYS).
    for key, schema in sorted(schemas.items()):
        if is_fragment(key):
            continue  # fragments MUST stay open so extenders can add fields
        if not any(schema.get(k) is False for k in STRICTNESS_KEYS):
            print(
                f"FAIL schema/{key}.json: must set additionalProperties or "
                "unevaluatedProperties to false (see README 'Rules')"
            )
            failures += 1

    return failures


def main() -> int:
    expected_version = int((ROOT / "VERSION").read_text().strip())

    schemas = {rel_key(p, SCHEMA_DIR): load(p) for p in sorted(SCHEMA_DIR.rglob("*.json"))}
    if not schemas:
        print("FAIL: no schemas found")
        return 1
    bad_schemas = 0
    for key, schema in schemas.items():
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:
            print(f"FAIL schema/{key}.json is not a valid Draft 2020-12 schema: {e}")
            bad_schemas += 1
    if bad_schemas:
        # Keep going: a caller fixing several schemas wants the whole list, not the first.
        print(f"({bad_schemas} unparseable schema(s); fixture results below may be partial)")

    registry = build_registry(schemas)
    fixtures = sorted(FIXTURE_DIR.rglob("*.json"))
    if not fixtures:
        print("FAIL: no fixtures found")
        return 1

    failures = bad_schemas
    for fx in fixtures:
        name = fx.relative_to(FIXTURE_DIR).as_posix()
        msg = load(fx)
        mtype = msg.get("type")
        subdir = fx.parent.relative_to(FIXTURE_DIR).as_posix()
        key = f"{subdir}/{mtype}" if subdir != "." else str(mtype)
        schema = schemas.get(key)
        if schema is None:
            print(f"FAIL {name}: no schema/{key}.json for type={mtype!r}")
            failures += 1
            continue
        try:
            errors = sorted(
                validator_for(schema, registry).iter_errors(msg), key=lambda e: list(e.path)
            )
        except Exception as e:  # unresolvable $ref, or a schema jsonschema chokes on
            failures += 1
            print(f"FAIL {name} ({mtype}): could not be validated: {type(e).__name__}: {e}")
            continue
        if errors:
            failures += 1
            print(f"FAIL {name} ({mtype}):")
            for e in errors:
                loc = "/".join(map(str, e.path)) or "<root>"
                print(f"   - {loc}: {e.message}")
            continue
        # A fixture carrying a version must match VERSION, or the golden bytes
        # would silently describe a protocol nobody speaks.
        if "protocol_version" in msg and msg["protocol_version"] != expected_version:
            failures += 1
            print(
                f"FAIL {name}: protocol_version {msg['protocol_version']} "
                f"!= VERSION {expected_version}"
            )
            continue
        print(f"ok   {name} ({mtype})")

    failures += check_schema_rules(schemas, fixtures)

    print(
        f"\n{len(fixtures)} fixtures, {len(schemas)} schemas, "
        f"{failures} failure(s) (protocol v{expected_version})"
    )
    if not HAVE_REGISTRY:
        print("note: `referencing` unavailable - cross-file $ref is NOT validated")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
