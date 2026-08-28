# nori-protocol

The **single source of truth** for the JSON wire contract Nori robots and clients speak. Consumed as a **git submodule** so no repo owns a private copy.

It covers two layers (see [Why two layers](#why-two-layers)): the **daemon** contract, spoken by the robot's on-board motion daemon over its internal control port, and the **session** contract, spoken over the WebRTC data channel by the operator clients — the TypeScript `@nori/sdk`, the Python `nori-sdk` on PyPI — and by the robot's ROS 2 gateway.

- **Writing your own client:** `CLIENTS.md` — the transport, the handshake, and a minimal example.
- **Supporting more than one robot model:** `MODELS.md` — capability discovery, the descriptor-vs-`model` rule, the two base namespaces, and the per-model fixtures. Read this before branching on anything.

## Layout
```
schema/daemon/    the DAEMON contract: what a motion daemon speaks over its control port
schema/session/   the SESSION contract: what an operator client speaks over the data channel
fixtures/daemon/  golden instances, mirroring the schema tree
fixtures/session/
validate.py       validates every fixture against its schema, plus this repo's own rules
VERSION           current integer protocol_version
MODELS.md         how a client discovers what robot it is talking to (descriptor + capabilities)
CHANGELOG.md      when each field appeared, and whether it was additive
```

### Why two layers
On an **L2** these are two hops: the app speaks the *session* dialect over WebRTC to a Pi
bridge, and the bridge speaks the *daemon* dialect over NDJSON to the C++ daemon. The bridge
intercepts the session-only verbs (`video`, `call`, `record`, `policy_stream`) and synthesises
others (`camera_layout`, `daemon_status`) that no daemon could report about itself. Note
`link` is NOT intercepted: it is forwarded to the daemon, which is why it lives in `daemon/`.

On an **L3** the gateway collapses both roles into one process and speaks the session dialect
directly. So the session layer is not "the L3 protocol" — it is the layer the app has always
spoken, which simply had no schema until now. That gap is why the L3 stack was written against
a mock robot instead of this repo.

**The session layer is a superset, not a replacement.** `control`, `command`, `link`, `telemetry`,
`ack`, `action_status`, `error` and `perception` reach the daemon, so they are defined **once**,
under `schema/daemon/`, and a session client validates them there. Two of them are not
byte-identical end to end: the L2 bridge INJECTS `robot_mic_live`, `robot_local_mic_muted` and
`battery_percent` into every `telemetry` frame, and `command` is accepted in two shapes. Both
are covered by the one daemon schema rather than a session copy. `schema/session/` holds only
what never reaches a daemon, so nothing is duplicated between the two trees.

If a message ever needs to differ per layer, add a session-layer schema that extends the
daemon one by `$ref`ing a shared `_`-prefixed fragment — see **Rules** for why that indirection
is required rather than optional.

## Message types (v1)
| Direction | type | schema |
|---|---|---|
| C→D | `hello` | `schema/daemon/hello.json` |
| C→D | `control` (bundles `jog` / `action` / `reset` / `clutch`) | `schema/daemon/control.json` |
| C→D | `command` (`estop` / `reset_latch`) | `schema/daemon/command.json` |
| C→D | `bye` | `{"type":"bye"}` — no fields, no schema needed |
| D→C | `ack` | `schema/daemon/ack.json` |
| D→C | `telemetry` | `schema/daemon/telemetry.json` |
| D→C | `error` | `schema/daemon/error.json` |
| D→C | `alert` | `schema/daemon/alert.json` — idempotent operator-alert state keyed by `code`; L3 gateway emits it today, a daemon may |

### Proposed (additive, no version bump — not yet emitted by any robot)
| Direction | type | schema | notes |
|---|---|---|---|
| D→C | `perception` | `schema/daemon/perception.json` | structured world-state (`objects[{label,confidence,bbox?,xyz?,id?}]`) from an on-robot detector |

It validates under `validate.py` today (fixtures included) but is **PROPOSED**: the client side
is built, the robot side is pending. `control.action_id`, `action_status` and `control.pose`
graduated to finalized on 2026-08-25 — see the CHANGELOG.

### Session layer (client ⇄ bridge/gateway, over the WebRTC data channel)
Schema'd 2026-08-13. Every one of these was already in production on both stacks and specified
nowhere, which is where most of the 2026-08 audit divergences were found.

| Direction | type | schema | notes |
|---|---|---|---|
| C→R | `video` | `schema/session/video.json` | pause/resume, bitrate, or quality — exactly one per frame |
| C→R | `record` | `schema/session/record.json` | six canonical verbs + five legacy aliases still in the wild |
| C→R | `policy_stream` | `schema/session/policy_stream.json` | capability-gated |
| C→R | `call` | `schema/session/call.json` | capability-gated; two-way audio |
| C→R | `navigation` | `schema/session/navigation.json` | capability `named_navigation`; correlated map-bound waypoint operations |
| R→C | `navigation_status` | `schema/session/navigation_status.json` | direct replies and unsolicited goal lifecycle snapshots |
| C→R | `sensor_stream` | `schema/session/sensor_stream.json` | capability `sensor_streams`; opt-in LiDAR/IMU rates and LiDAR point bound |
| R→C | `sensor_stream_status` | `schema/session/sensor_stream_status.json` | correlated effective configuration and current ROS publisher presence |
| R→C | `lidar_scan` | `schema/session/lidar_scan.json` | sampled filtered `/scan`; invalid ROS values are `null` |
| R→C | `imu` | `schema/session/imu.json` | `/imu/data` quaternion, SI vectors and covariance arrays |
| R→C | `camera_layout` | `schema/session/camera_layout.json` | how to slice the single composited track; **not** derived from `descriptor.cameras` |
| R→C | `daemon_status` | `schema/session/daemon_status.json` | motion health as seen by the bridge — the daemon cannot report its own absence |
| R→C | `record_status` | `schema/session/record_status.json` | one per verb, no correlation id |
| R→C | `policy_stream_status` | `schema/session/policy_stream_status.json` | reply only — a stream that dies mid-run is observable **only by polling** |

## Rules
- **Framing:** newline-delimited JSON over TCP (TLS on WAN). One object per line, UTF-8.
- **Versioning:** integer `protocol_version`, asserted in `hello`/`ack`; mismatch ⇒ hard reject. Additive changes do **not** bump the version; a rename/retype/removal bumps it and must break CI here (golden fixtures). *Caveat: hard rejection is the `hello` negotiation, which only the L2 daemon performs. The L3 gateway sends `ack` unprompted with no `hello` and no negotiation, and both SDKs treat a version mismatch as advisory. Reconcile before relying on rejection as a safety property.*
- **Message schemas are strict** — `additionalProperties: false`, or `unevaluatedProperties: false` when the schema extends another — so drift is caught in CI. `validate.py` enforces this. **Runtime parsers must be lenient** — ignore unknown fields — so a future additive field doesn't break an older binary at runtime.
- **Shared definitions live in `_`-prefixed fragments**, which are exempt from the strictness and fixture-coverage rules because they must stay OPEN. This shape is forced by how JSON Schema composition actually behaves, and two plausible-looking alternatives silently fail: `additionalProperties: false` on a base rejects an extender's new fields (it cannot see properties declared outside its own subschema), and `unevaluatedProperties: false` on a base is no better, since inside a `$ref` it evaluates in its own scope and rejects them too — then, because the base failed, its annotations are dropped and the extender sees nothing as evaluated. The working shape is an open `_core` fragment plus `allOf: [{$ref: _core}], properties: {…}, unevaluatedProperties: false` on each concrete message.
- **A `_`-prefixed fragment MUST declare `$id`.** `$ref` is resolved through a registry keyed on it; without one, every schema extending the fragment fails to resolve at validation time. `validate.py` fails the build for it rather than crashing.
- **Every message schema needs at least one fixture.** An unexercised schema is an untested one; `validate.py` fails the build for it.
- **Units (lerobot-native):** joint `<motor>.pos` normalized [-100,100], gripper [0,100]; base `x.vel` m/s / `theta.vel` **deg/s**; `jog` axes normalized rate [-1,1]; currents signed raw int. The active normalization mode is declared in `ack.norm_mode`. (`left_lift`/`right_lift` are a Nori extension, present only in `jog` — one velocity-mode lift per arm, not in the lerobot `.pos`/`.vel` dicts.)

## CI
```bash
python validate.py        # exits non-zero on any schema/fixture mismatch
```
Consumers: the robot's on-board daemon, the browser app, the robot's ROS 2 gateway, and both operator SDKs (`@nori/sdk`, `nori-sdk`) — each running `validate.py` plus their own golden-fixture decode tests against `fixtures/`.

> **Status (2026-08-26): all four stacks consume this repo.** Both SDKs run fixture-driven
> conformance suites against it in CI (the Python SDK pins its base-jog emission
> byte-for-byte against `fixtures/daemon/control_jog_base.json`), and the robot's gateway
> ships its own conformance tests. The 2026-08 audits found every wire divergence traced to
> consumers that had drifted from this spec — reattaching them fixed the lot.
