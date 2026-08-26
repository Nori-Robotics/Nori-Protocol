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
| `task_jog` — cylindrical/task-space arm jog | yes | yes (base-frame twist; `yaw` (canonical) / `shoulder_pan` (deprecated alias) yaws about base Z) |
| `record` — dataset recording verbs | yes | yes (same 11 verbs; `discard` differs in MEANING — see session/record.json) |
| `leader_action_deg` — absolute leader degrees | yes | **no** — dropped as unmappable |
| `lift_targets` — absolute `<side>_lift.pos` in mm | yes | **no** — silently ignored |
| `policy_stream` | yes | **no** |
| `call` — two-way audio | yes | **no** |
| `perception` | schema only, unimplemented | **no** |
| `pose_targets` — absolute Cartesian pose targets, solved on-robot (`control.pose`) | **no** | **yes** — finalized 2026-08-25; the nori_ws gateway advertises and serves it whenever its motion stack is up (bench-verified on A3 through the full lifecycle). Still gate on the capability: a robot without it drops the frame in silence |

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

> **Status: reconciled (2026-08-25).** `fixtures/daemon/control_jog_base.json`
> now describes what every client emits and what the gateway consumes: no
> negation on either side. The one surviving exception is the DEPLOYED L2
> fleet, whose frozen firmware turns opposite on `angular` — that flip lives
> client-side in the TS SDK's `RemoteTeleop.wireJog`, behind a positive L2
> model match (never a fallback), because the L2 daemon cannot host the
> actuator-adapter compensation this section prescribes. Rollout coupling:
> a client from before this change negates `angular` against a gateway from
> after it (and vice versa) — update gateway and clients together.

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
| `task` | m/s (x, y, z), rad/s (pitch, yaw — canonical; shoulder_pan is a deprecated alias of yaw) | nothing — Cartesian verbs have no position key |
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

## Real angles from a normalized wire — `descriptor.ranges_si`

`ranges` is in `norm_mode` units, and the mapping from those to physical units is the robot's
own **per-unit calibration** — not a nominal figure from the URDF. So a client that wants real
angles (to pose a URDF, run its own FK, feed a simulator) has no exact way to get them: it can
substitute the URDF's nominal joint limits, which are wrong by whatever that unit's calibration
offset happens to be, and wrong **silently**.

`ranges_si` publishes the calibrated bounds — radians for a revolute joint, metres for a
prismatic one — so the conversion becomes exact.

**Convert by mapping linearly between the two entries for the same key.** Do not special-case
the gripper: `ranges` already carries the convention difference (a body joint is `[-100, 100]`,
a gripper is `[0, 100]`), so one formula covers both.

```
frac = (value - ranges[k][0]) / (ranges[k][1] - ranges[k][0])
si   = ranges_si[k][0] + frac * (ranges_si[k][1] - ranges_si[k][0])
```

That is the same arithmetic the daemon runs in the other direction, so a round trip is lossless
up to float precision.

**Only keys whose `ranges` entry is normalized appear.** A key already published in physical
units must NOT have an entry — the A-series lift is the case in point: `ranges["lift.pos"]` is
already millimetres, so an SI entry would mean converting twice.

**Bounds may be inverted** (`lower > upper`) where the calibration reverses that axis. Do not
sort them: the order carries the direction, and normalizing it away flips the joint.

Absent means UNKNOWN, per key and for the whole map. The L-series fleet is frozen and will
never publish it, so a client that needs real angles must handle absence — the honest fallback
is to say "unknown", not to substitute nominal limits.

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
