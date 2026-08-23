# Changelog

Wire-contract changes, newest first. Every entry states whether it is **additive**
(no `protocol_version` bump; older clients keep working by ignoring the new field)
or **breaking** (bumps `VERSION` and must break every consumer's CI).

`VERSION` is currently **1**.

This file exists because "when did this field appear, and which robots have it?"
turned out to be the question that costs the most to answer after the fact.

## 2026-08-20 — `alert` frames schema'd (additive, retroactive)

The L3 gateway began emitting robot-owned operator alerts (`servo_thermal`, with the
thermal-supervisor stack) before any schema existed — a whole new message TYPE with no
contract, one level up from the `servo_temps` field drift of 2026-08-12, and caught the
same way: the gateway's conformance suite (`nori_ws` `test_conformance.py`) pinned the gap
as a strict xfail on the day of the merge.

- **`schema/daemon/alert.json`** — schema'd descriptively from the gateway's emitter.
  `alert` is idempotent STATE keyed by `code` (`active: false` clears it), not a toast:
  the control channel is unreliable and unordered, so clients must dedupe/replace by
  `code`, never count frames. `severity` is the only rendering input; condition-specific
  context rides optional fields (`hottest_c`, `joint`, `latched`), so a new alert kind is
  a new `code` value, not a new message type. Unknown codes MUST render generically, not
  error. Lives under `daemon/` because a daemon can legitimately originate one (thermal),
  even though the L3 gateway is today's only emitter.
- New fixtures: `alert_thermal_latched.json`, `alert_cleared.json`.

## 2026-08-20 — `descriptor.jog_scale` (additive)

Recorded retroactively (the commit landed without an entry — the "when did this field
appear" failure this file exists to prevent, in the file itself). `ack.descriptor` gains
an optional `jog_scale` block advertising the nominal commanded scale of a full-deflection
jog per namespace (joints / task / base / lift), so a script can convert a desired real
rate into a normalized one and verify against telemetry. Omission at every level means
UNKNOWN, never zero; the L2 fleet is frozen and will never send it. See MODELS.md
"Knowing how fast 1.0 is". New fixtures: `ack_a3_jog_scale.json`,
`ack_jog_scale_partial.json`.

## 2026-08-13 — command hardening and secret hygiene (constraints + docs only)

No new message types and no field renames. A pre-commit security pass over the day's work.

- **`daemon/command.json` now documents that the value is never read.** The L2 broker decides
  by SUBSTRING on the raw line, so a command frame merely *containing* the text triggers the
  action:

  ```
  {"estop": false}        trips the e-stop   (annoying, but fail-safe)
  {"reset_latch": false}  CLEARS an e-stop   (fail-DANGEROUS)
  ```

  Only the first was written down earlier that day; the second is the one that matters. A
  client that serialises every field with an explicit `false`, or that puts the word in a
  note, silently un-latches a stopped robot. Both flags are pinned `const: true` so a
  conforming client cannot express it — send the key or omit it, never send it false.

- **The `token` requirement on `reset_latch` was dropped, and its absence recorded.** The
  schema previously required a token alongside `reset_latch`, which reads as an auth control
  but was never enforced anywhere: the daemon parses `token` only out of `hello`, and the
  broker reads none here at all. Removing it makes the schema descriptive — but it also
  removed the last thing that even *implied* the control existed, so the description now
  states the fact plainly: **clearing an E-STOP is unauthenticated at this layer.** Any
  client already admitted to a session can do it. The real gate is upstream, in the Supabase
  RLS policy that decides who may join the signaling room. That may well be the intended
  design; it is now written down rather than hinted at.

- **`fixtures/session/record_status_l2.json` no longer carries a real-looking fleet serial.**
  The session and episode ids were transcribed verbatim from the recorder's own example and
  contained a plausible unit number. `CLIENTS.md` invites external client authors into this
  repo, so golden fixtures use an obvious placeholder (`-0000`) instead.

## 2026-08-13 — corrections from an adversarial review of the session layer

The session schemas below were written against the L3 gateway and the TypeScript client, and
verified against L3 output only. An audit against the **L2 daemon, bridge and recorder** found
that several rejected what deployed L2 units actually send. No robot changed; the schemas were
wrong. Everything here is descriptive: **no deployed unit is expected to conform to anything it
did not already do.**

Rejected real L2 output, now accepted:
- `telemetry` — the L2 bridge injects `robot_mic_live`, `robot_local_mic_muted` and
  `battery_percent` into every relayed frame. This is the `servo_temps` failure repeating
  inside the commit that documented `servo_temps`.
- `telemetry.status` no longer requires `link` (L2-only; L3's `safety_status()` never sets it,
  so requiring it rejected every L3 frame carrying a status block).
- `record_status` — added `session`, `dropped_frames_est`, `queue_drops`; `episode` now accepts
  BOTH `episode-NNNN` (L3) and `<session>/episode-NNNN` (L2); `error` no longer required when
  `ok` is false, since L3 can omit an empty service message.
- `daemon/command` — accepts the flag form `{"estop": true}` that every deployed client sends,
  alongside the original `{name, token}`. The separate `session/command.json` added earlier
  was deleted: its stated justification (bridge translation, token injection) does not happen.
- `control` no longer requires `seq`. The L2 daemon defaults it to `-1` and both clients omit
  it on reset-only frames.
- `call` — added `robot_mic_muted`, which the L2 bridge implements.

Corrected claims (documentation only, but two were dangerous):
- **`record` verb semantics.** The previous text said `discard`/`discard_last`/`session_discard`
  are "all treated as session_end". On L2 they `shutil.rmtree` the session; `session_end` keeps
  it for shipping. **They are opposites.** `stop` on L2 also ends the session, not just the
  episode, and `session_discard` is canonical rather than a legacy alias. A client built on the
  old wording could destroy recorded data believing it was closing a session cleanly.
- **`{"estop": false}` trips the e-stop** on L2 — the broker substring-matches the raw line.
  Previously described only as "ambiguous".
- `policy_stream_status` is reply-only; there is no unsolicited death notification (the L2
  streamer is a ZMQ REP socket). A dying stream is observable only by polling.
- The control channel is **unordered as well as unreliable** on both stacks, so FIFO waiter
  matching has no transport guarantee.
- `daemon_status.reason` is an open set; the L2 bridge's most common value is `connection_lost`.
- `camera_layout` no longer contradicts itself about empty `tiles`.

Tooling and process:
- `validate.py` — an unresolvable `$ref` is now a FAIL line instead of an unhandled traceback;
  one unparseable schema no longer aborts the whole run; and a `_`-prefixed fragment without
  `$id` is diagnosed, which is what the documented fragment pattern actually requires (the
  worked example in the README omitted it and crashed).
- **New L2-shape fixtures** (`telemetry_l2_bridged`, `record_status_l2`,
  `daemon_status_connection_lost`, `telemetry_l3_status`, `record_status_no_error`). These are
  the durable fix: verifying by hand against one stack is what caused all of the above, so the
  shapes now live in CI.

## 2026-08-13 — daemon/session split; the session layer schema'd (additive)

No wire change: every message below was already in production on both stacks. They were
simply specified nowhere, because this repo was deliberately scoped to the daemon contract
back when the bridge and the daemon were separate processes. The L3 gateway merged those
roles, so the unspecified half is now most of what a client actually speaks — and it is where
most of the 2026-08 audit divergences were found.

- Existing schemas and fixtures moved to `schema/daemon/` and `fixtures/daemon/` (`git mv`,
  history preserved). Their `$id`s gained the `/daemon/` segment.
- **Eight new session schemas**: `camera_layout`, `daemon_status`, `video`, `record`,
  `record_status`, `policy_stream`, `policy_stream_status`, `call`. (`link` was initially
  filed here too and moved to `daemon/` the same day — it is forwarded to the daemon, not
  intercepted, so it fails this repo's own criterion for session membership.)
- Notable things now pinned rather than tribal: `link` selects the robot's watchdog profile
  and is therefore a safety input; `camera_layout` is authoritative for video tiling and is
  NOT derived from `descriptor.cameras`; `record` replies carry no correlation id, so verbs
  must not be pipelined; `record`'s five legacy aliases (`start`, `stop`, `discard`,
  `discard_last`, `session_discard`) are documented as retained-for-deployed-clients;
  `policy_stream_status` arrives unsolicited when a stream dies, and its `ok` defaults to
  false rather than true.
- Verification at the time was **L3 output only**, and that turned out to be the flaw rather
  than the assurance — see the correction entry above. Nothing about the L2 bridge or the L2
  recorder was checked, which is where every subsequent finding was.

## 2026-08-13 — validator hardening + CI

Tooling only; no wire change.

- `validate.py` now also enforces this repo's own stated rules: every message
  schema must be exercised by at least one fixture, and every message schema must
  close itself (`additionalProperties` or `unevaluatedProperties` set to `false`).
  An unexercised schema is an untested one, which is how a message type rots.
- `validate.py` walks subdirectories and resolves cross-file `$ref` through a
  `referencing` registry, so a `daemon/` + `session/` split needs no further
  tooling change. Files whose name starts with `_` are treated as reusable
  fragments: still parsed, but exempt from the coverage and strictness rules.
- Added `.github/workflows/validate.yml`. Before this, `validate.py` ran in no
  CI anywhere, in any repo.

Note what this does **not** cover: implementation drift. It proves the schemas
and fixtures agree with each other, not that any daemon or client agrees with
them. See the 2026-08-12 `servo_temps` entry for what that costs.

## 2026-08-13 — `model`, `capabilities`, L3 joint vocabulary (additive)

Landed as `40dd3bc`.

- **`ack.model`** — advisory model/generation label (`"L2"`, `"L3"`). Deliberately
  a free string, not an enum: enumerating models here would mean a schema edit per
  model, which is the coupling the field exists to avoid. Clients MUST NOT branch
  on it; branch on `descriptor` and `capabilities`. See `MODELS.md`.
- **`ack.capabilities`** — optional verbs a robot honours beyond the core, since
  capability differences (absolute lift targets, leader degrees, two-way audio,
  policy streaming) are not expressible as DOFs. Absent means *unknown*, not
  *none*. Unrecognised entries must be ignored, not rejected.
- **`control.jog.<arm>` reopened.** It was `additionalProperties: false` over the
  six L2 task-space DOFs, which made every L3 joint (`shoulder_pitch`,
  `bicep_yaw`, `elbow_pitch`, …) schema-**invalid** — the canonical spec could not
  describe an L3 arm at all. It now accepts descriptor-declared joint shorts
  alongside the task-space verbs, with the routing rule documented.
- **Base sign convention made normative**: `+linear` forward, `+angular` left
  (ROS REP-103), no client-side negation, robots compensate internally. Both
  current implementations deviate; see `MODELS.md` for the reconciliation, and
  treat `fixtures/daemon/control_jog_base.json` as the target rather than a description
  of today.
- **`telemetry.servo_temps`** — see below.
- New docs: `MODELS.md`. New fixtures: `ack_l2_legacy.json` (no descriptor, which
  is what the L2 daemon actually sends), `ack_l3.json`, `control_jog_base.json`,
  `control_jog_l3_joints.json`.

## 2026-08-12 — `telemetry.servo_temps` schema'd (additive, retroactive)

The L2 daemon began emitting `servo_temps` on **2026-08-09**; the schema was last
touched 2026-07-22 and is `additionalProperties: false`, so for three weeks real
current L2 telemetry **failed its own schema** and nothing noticed, because
`validate.py` ran in no CI and no consumer fed real output through it.

Recorded here as the worked example of the failure mode this repo exists to
prevent: a field is a wire change even when it is "just telemetry".

## Earlier

See `git log`. Entries before 2026-08-12 predate this file.
