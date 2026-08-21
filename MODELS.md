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
   *none*: a robot predating the field must be assumed legacy. The list is a
   UNION: the daemon lists what it honours and a bridge appends what it adds
   before forwarding, so `fixtures/daemon/ack_l2_legacy.json` shows the
   post-bridge set a client actually receives, not what the C++ daemon alone
   emits. Do not infer which component provides a capability.

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
| `record` — dataset recording verbs | yes | yes (same 11 verbs; `discard` differs in MEANING — see session/record.json) |
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
> `fixtures/daemon/control_jog_base.json` is the target, not a description of what
> either side currently emits.

## Knowing how fast "1.0" is — `descriptor.jog_scale`

A jog rate is normalized `[-1, 1]`, which is what makes one client work across models: the
robot owns the mapping from "full deflection" to real motion, and a client that compiled in
rad/s would be compiling in a per-model constant. The cost is that a script cannot ask for a
*repeatable* speed, and cannot discover what it just asked for.

`descriptor.jog_scale` closes that without adding a velocity command. It advertises the
multiplier, so a client can convert its own desired rate into a normalized one and verify the
result against telemetry.

**It promises the NOMINAL COMMANDED scale, not achieved velocity.** Three things routinely
make the real rate lower, and a client must expect all three:

- the watchdog's `warn` state scales all motion to **zero** until control frames resume —
  observable as `telemetry.status.watchdog`;
- an acceleration limit means a short jog never reaches the rate at all;
- on stacks running MoveIt Servo, singularity and collision scaling reduce it near limits.

**Units are per namespace, and each matches the thing it addresses** so a client can close the
loop:

| Namespace | Unit | Matches |
|---|---|---|
| `joints` | norm_mode units/s | `telemetry.state` and `descriptor.ranges` |
| `lift` | mm/s | the mm of `<side>_lift.pos` absolute targets |
| `task` | m/s (x, y), rad/s (pitch, shoulder_pan) | nothing — Cartesian verbs have no position key |
| `base` | m/s, rad/s | keyed `linear`/`angular`, the JOG namespace |

Joints are deliberately **not** in rad/s. Telemetry reports normalized positions, so a rate in
rad/s could not be checked against anything a client can see — and the whole point is
verifiability. Expect the per-joint numbers to differ even where a robot uses one internal
rad/s constant, because the normalized-to-physical scale is per joint.

Omission means UNKNOWN, never zero — at every level. A missing block, a missing namespace and
a missing joint key all mean "not advertised", and a client must not substitute a default. A
rate of `0` is invalid rather than meaning "cannot move": a joint that cannot be jogged is
expressed by leaving it out.

**The L2 fleet is frozen and will never send this**, so absent is the common case and will
stay that way. Any client using `jog_scale` needs a path for robots that do not publish it.

## Cameras are not in the descriptor path

`descriptor.cameras` is diagnostic metadata. It does **not** drive video.

A robot composites all of its cameras into one frame and sends a single H.264
track; the tiling is described by `camera_layout` (`cols`, `rows`, `tiles`),
which is authoritative and independent of the descriptor. Clients crop tiles
from the composite using that frame. This is why L2 and L3 behave identically in
the app despite L2 sending no descriptor at all, and why a mock must NOT derive
its layout from `descriptor.cameras` — real robots derive it from the live
compositor. This is a rule to adopt, not a description of today: the TS mock
(`mock/sim.ts` cameraLayoutFrame) derives its layout from the descriptor, and the
L3 gateway was written against that mock.

## Current models

**L2** — 12 motors, 5-DOF arms plus gripper
(`shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper`),
two dedicated lift servos with a millimetre height frame, `hello` handshake with
hard version rejection, single-client NDJSON arbitration. Sends no descriptor.
Fixture: `fixtures/daemon/ack_l2_legacy.json`.

**L3** — 16 joints, 7-DOF arms plus gripper
(`shoulder_pitch, shoulder_roll, bicep_yaw, elbow_pitch, forearm_yaw,
wrist_pitch, wrist_roll, gripper`), lifts implemented as arm shoulder-lift
joints (jog only), no `hello` (auth moved upstream to the signaling room),
authority-lease arbitration. Fixture: `fixtures/daemon/ack_l3.json`.

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
