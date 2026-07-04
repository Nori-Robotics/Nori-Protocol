# nori-protocol

The **single source of truth** for the JSON wire contract between the Nori C++ on-board daemon (`NoriCoreAgent`, in `NoriTeleop/rpi5/`) and the laptop app (`NoriLeLab`). Consumed by both as a **git submodule** so neither repo owns a private copy.

- **Writing your own client:** `CLIENTS.md` — the transport, the handshake, and a minimal example.
- Human spec / rationale: `NoriTeleop/docs/nori_protocol_schema.md`.
- Architecture context: `onboard_pi_plan.md` §f / R8.

## Layout
```
schema/      JSON Schema (Draft 2020-12), one file per message type — the canonical definition
fixtures/    golden message instances — same bytes must decode identically in both repos
validate.py  validates every fixture against its schema (run in both repos' CI)
VERSION      current integer protocol_version
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

## Rules
- **Framing:** newline-delimited JSON over TCP (TLS on WAN). One object per line, UTF-8.
- **Versioning:** integer `protocol_version`, asserted in `hello`/`ack`; mismatch ⇒ hard reject. Additive changes do **not** bump the version; a rename/retype/removal bumps it and must break CI here (golden fixtures).
- **Schemas are strict** (`additionalProperties: false`) so drift is caught in CI. **Runtime parsers must be lenient** — ignore unknown fields — so a future additive field doesn't break an older binary at runtime.
- **Units (lerobot-native):** joint `<motor>.pos` normalized [-100,100], gripper [0,100]; base `x.vel` m/s / `theta.vel` **deg/s**; `jog` axes normalized rate [-1,1]; currents signed raw int. The active normalization mode is declared in `ack.norm_mode`. (`left_lift`/`right_lift` are a Nori extension, present only in `jog` — one velocity-mode lift per arm, not in the lerobot `.pos`/`.vel` dicts.)

## CI
```bash
python validate.py        # exits non-zero on any schema/fixture mismatch
```
Both `NoriTeleop` and `NoriLeLab` run this plus their own golden-fixture decode tests against `fixtures/`.
