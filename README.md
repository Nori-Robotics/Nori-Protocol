# nori-protocol

The **single source of truth** for the JSON wire contract between the Nori C++ on-board daemon (`NoriCoreAgent`, in `NoriTeleop/rpi5/`) and the laptop app (`NoriLeLab`). Consumed by both as a **git submodule** so neither repo owns a private copy.

- **Writing your own client:** `CLIENTS.md` — the transport, the handshake, and a minimal example.
- **Supporting more than one robot model:** `MODELS.md` — capability discovery, the descriptor-vs-`model` rule, the two base namespaces, and the per-model fixtures. Read this before branching on anything.
- Human spec / rationale: `NoriTeleop/docs/nori_protocol_schema.md`.
- Architecture context: `onboard_pi_plan.md` §f / R8.

## Layout
```
schema/      JSON Schema (Draft 2020-12), one file per message type — the canonical definition
fixtures/    golden message instances — same bytes must decode identically in both repos
validate.py  validates every fixture against its schema (run in both repos' CI)
VERSION      current integer protocol_version
MODELS.md    how a client discovers what robot it is talking to (descriptor + capabilities)
```

## Message types (v1)
| Direction | type | schema |
|---|---|---|
| C→D | `hello` | `schema/hello.json` |
| C→D | `control` (bundles `jog` / `action` / `reset` / `clutch`) | `schema/control.json` |
| C→D | `command` (`estop` / `reset_latch`) | `schema/command.json` |
| C→D | `bye` | `{"type":"bye"}` — no fields, no schema needed |
| D→C | `ack` | `schema/ack.json` |
| D→C | `telemetry` | `schema/telemetry.json` |
| D→C | `error` | `schema/error.json` |

### Proposed (additive, no version bump — not yet emitted by the daemon)
| Direction | type | schema | notes |
|---|---|---|---|
| C→D | `control.action_id` (optional field) | `schema/control.json` | correlates a motion frame to its `action_status` |
| D→C | `action_status` | `schema/action_status.json` | Phase E / G1 — action-completion lifecycle (`accepted→active→done\|blocked\|clamped\|timeout`) |
| D→C | `perception` | `schema/perception.json` | Phase F / G3 — structured world-state (`objects[{label,confidence,bbox?,xyz?,id?}]`) from an on-Pi detector |

These validate under `validate.py` today (fixtures included) but are **PROPOSED**: the app/SDK side
is built, the daemon side is pending (see `docs/daemon_followups.md` in the NoriTeleop repo).

## Rules
- **Framing:** newline-delimited JSON over TCP (TLS on WAN). One object per line, UTF-8.
- **Versioning:** integer `protocol_version`, asserted in `hello`/`ack`; mismatch ⇒ hard reject. Additive changes do **not** bump the version; a rename/retype/removal bumps it and must break CI here (golden fixtures). *Caveat: hard rejection is the `hello` negotiation, which only the L2 daemon performs. The L3 gateway sends `ack` unprompted with no `hello` and no negotiation, and both SDKs treat a version mismatch as advisory. Reconcile before relying on rejection as a safety property.*
- **Schemas are strict** (`additionalProperties: false`) so drift is caught in CI. **Runtime parsers must be lenient** — ignore unknown fields — so a future additive field doesn't break an older binary at runtime.
- **Units (lerobot-native):** joint `<motor>.pos` normalized [-100,100], gripper [0,100]; base `x.vel` m/s / `theta.vel` **deg/s**; `jog` axes normalized rate [-1,1]; currents signed raw int. The active normalization mode is declared in `ack.norm_mode`. (`left_lift`/`right_lift` are a Nori extension, present only in `jog` — one velocity-mode lift per arm, not in the lerobot `.pos`/`.vel` dicts.)

## CI
```bash
python validate.py        # exits non-zero on any schema/fixture mismatch
```
**Intended** consumers: `NoriTeleop`, `NoriLeLab`, `nori_ws` (the L3 ROS 2 gateway) and `nori-sdk-py`, each running this plus their own golden-fixture decode tests against `fixtures/`.

> **Actual status (2026-08-12): only `NoriTeleop` consumes this repo.** `NoriLeLab` has no
> `.gitmodules` entry, `nori_ws` has no reference to it, and neither runs `validate.py`. Every
> wire divergence found in the 2026-08 audits — the base DOF namespace, the action lifecycle,
> `policy_stream_status` field names, the L3 joint vocabulary being schema-invalid — is
> downstream of that. Re-attaching the two drifted consumers is worth more than any single
> schema fix here.
