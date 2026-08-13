# Robot models and capability discovery

How a client works out what it is talking to, and what it may do with it.

## The rule

**Branch on `descriptor` and `capabilities`. Never on `model`.**

A client that switches behaviour on the string `"L3"` needs editing every time a
model ships. A client that reads the descriptor works against hardware that did
not exist when it was written. `model` is in the `ack` so that logs, dataset
provenance and bug reports can *name* the hardware — it is not a control input.

This is the same conclusion the backend reached in migration 034 (`robots.model`
exists precisely so callers stop re-parsing the serial) and the same one
`nori_protocol_schema.md` states for the descriptor: *"the `descriptor` lets the
app adapt instead of hardcoding DOF."*

## Discovery, in order

1. **`descriptor` absent** → a legacy robot whose DOF vocabulary the client must
   already know. The L2 daemon is one: it sends `norm_mode`, `watchdog_profile`
   and `initial_state`, and no descriptor at all. Treat this as "L2 legacy" and
   use the compiled-in 5-DOF map. This fallback is the ONLY place a hardcoded
   joint list is legitimate.
2. **`descriptor` present** → build the keymap from `descriptor.joints`. Strip
   the `<side>_arm_` prefix and `.pos` suffix to get the jog short name. Do not
   assume any particular joint exists.
3. **`capabilities`** → gate optional verbs. Absent field means *unknown*, not
   *none*: a robot predating the field must be assumed legacy.

`ranges` scales your input; it does not validate it. Out-of-range values are
clamped robot-side and reported via `action_status: clamped`, never rejected.

## Why capabilities exist as a separate list

A descriptor can only express **degrees of freedom**. It cannot express whether
a robot accepts absolute lift height targets, honours leader-arm degrees, runs a
policy streamer, or carries two-way audio. Those differences are large between
current models and a client has no other way to discover them:

| Capability | L2 | L3 |
|---|---|---|
| `task_jog` — cylindrical/task-space arm jog | yes | yes (base-frame twist; `shoulder_pan` yaws about base Z) |
| `record` — dataset recording verbs | yes | yes (richer verb set) |
| `leader_action_deg` — absolute leader degrees | yes | **no** — dropped as unmappable |
| `lift_targets` — absolute `<side>_lift.pos` in mm | yes | **no** — silently ignored |
| `policy_stream` | yes | **no** |
| `call` — two-way audio | yes | **no** |
| `perception` | schema only, unimplemented | **no** |

Unknown entries MUST be ignored rather than treated as errors, so a newer robot
can advertise capabilities this client has never heard of.

## The two base namespaces

This has bitten more than one implementation, so it is stated twice in the
schemas and once here.

- **Telemetry / descriptor** name the base axes `x.vel` and `theta.vel`.
- **Jog** names them `linear` and `angular`.

They are deliberately different — telemetry reports measured axis velocity, jog
expresses normalized drive intent — and they are not interchangeable. A jog frame
carrying `{"base": {"x": 1.0, "theta": 0.5}}` is schema-invalid, and a robot that
receives it parses `linear`/`angular` as absent, i.e. **zero**, and stops the
base. Silently.

### Sign convention (normative)

`+linear` drives **forward**. `+angular` turns **left** (counter-clockwise,
ROS REP-103). **No client-side negation.**

A robot whose hardware turns the other way compensates in its own actuator
adapter, internally. Never on the wire, and never in a shared client library —
a per-model quirk fixed in shared code becomes every other model's bug.

> **Status: both current implementations deviate and must be reconciled.** The
> L2-era app negated `angular` on the wire and the L3 gateway un-negates it to
> compensate; a later app change began negating `linear` as well, which the
> gateway does not undo. Adopting this convention means the app stops negating
> and the L3 gateway stops un-negating, in one coordinated change. Until then,
> `fixtures/control_jog_base.json` is the target, not a description of what
> either side currently emits.

## Cameras are not in the descriptor path

`descriptor.cameras` is diagnostic metadata. It does **not** drive video.

A robot composites all of its cameras into one frame and sends a single H.264
track; the tiling is described by `camera_layout` (`cols`, `rows`, `tiles`),
which is authoritative and independent of the descriptor. Clients crop tiles
from the composite using that frame. This is why L2 and L3 behave identically in
the app despite L2 sending no descriptor at all, and why a mock must NOT derive
its layout from `descriptor.cameras` — real robots derive it from the live
compositor, and a mock that does otherwise hides layout bugs.

## Current models

**L2** — 12 motors, 5-DOF arms plus gripper
(`shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper`),
two dedicated lift servos with a millimetre height frame, `hello` handshake with
hard version rejection, single-client NDJSON arbitration. Sends no descriptor.
Fixture: `fixtures/ack_l2_legacy.json`.

**L3** — 16 joints, 7-DOF arms plus gripper
(`shoulder_pitch, shoulder_roll, bicep_yaw, elbow_pitch, forearm_yaw,
wrist_pitch, wrist_roll, gripper`), lifts implemented as arm shoulder-lift
joints (jog only), no `hello` (auth moved upstream to the signaling room),
authority-lease arbitration. Fixture: `fixtures/ack_l3.json`.

Only `wrist_roll` and `gripper` are common to both vocabularies. That near-zero
overlap is why descriptor-driven keymapping works in practice — but it is a
happy accident, not a guarantee, which is why the absent-descriptor fallback is
specified explicitly above rather than left to inference.

## Conformance gaps in current implementations

The fixtures here are the target. These are the known deltas:

- **L3 `descriptor.aux` is `[]`** while the robot accepts `left_lift`/`right_lift`
  jog and reports `<side>_lift.pos` telemetry. The lifts are undiscoverable. Fix
  the gateway to populate `aux`.
- **L3 emits no `descriptor.cameras` and no `descriptor.buses`.** `cameras` is
  cheap to add from the compositor roles and useful in logs; `buses` may stay
  absent.
- **Neither stack emits `model` or `capabilities`** — both fields are new here.
- **Base sign convention** — see above.
- **The app and the L3 gateway consume no schema at all.** `validate.py` is
  wired into neither. `nori-protocol` is currently a submodule of `NoriTeleop`
  only, which is how all of the above drifted.

## Versioning

`model` and `capabilities` are **optional additions to v1**, not a breaking
change: a client that ignores them behaves exactly as before, and a robot that
omits them is handled by the "absent means unknown/legacy" rules. Same reasoning
as the `left_lift.pos` addition of 2026-07-02 — additive under existing
patterns, no `protocol_version` bump. `VERSION` stays at 1.
