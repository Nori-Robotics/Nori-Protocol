# Writing your own client

This is the **transport + session guide** — how to open a connection to the Nori daemon and run a
teleop session in any language. The message *shapes* are defined by the JSON Schemas in `schema/`
(with golden examples in `fixtures/`); this doc covers everything around them: the socket, the
handshake, the loop, and the safety model.

> If you're writing a **TypeScript / browser** client, you probably want the reference SDK
> (`@nori/sdk`) instead of talking to the socket by hand — it does the WebRTC transport, the
> handshake, and the jog streaming for you. This guide is for **your own client in your own stack**
> (Python, ROS, Rust, Unity, …).

## The connection

The daemon is a **line-delimited-JSON TCP server**:

- **Address:** `robot-host:7777` (the daemon's control port; `TCP_PORT` in the daemon).
- **Framing:** one JSON object per line, `\n`-terminated, UTF-8. Same in both directions.
- **One client at a time.** The daemon serves a single control client. If the robot's WebRTC bridge
  (`webrtc_robot.py`) is running and connected, *it* is that client — stop it before connecting your
  own, or connect through it (see [Remote / WAN](#remote--wan) below).
- **Direct TCP is for trusted networks** — on the robot itself, or a LAN/VPN you control. It is not
  meant to be exposed to the open internet as-is (see Remote / WAN).

## Session lifecycle

```
  connect TCP :7777
        │
        ▼
  ── hello ──────────────►        first line MUST be a hello (schema/daemon/hello.json)
        ◄──────── ack ──          accepted:true (+ descriptor on robots that send one; the L2 daemon does not), or accepted:false + error (schema/daemon/ack.json)
        │
        ▼   ┌─────────────────────────────────────────────┐
   loop │   │  ── control ─────►   jog frames @ ~50 Hz     │  keep sending, or the watchdog safe-stops
        │   │  ── command ────►   estop / reset_latch      │
        │   │  ◄──── telemetry ─   state + currents        │  streamed continuously
        │   │  ◄──── error ────    non-fatal notices        │
        │   └─────────────────────────────────────────────┘
        ▼
  ── bye ────────────────►        clean shutdown ({"type":"bye"})
```

1. **`hello`** (client → daemon) — the first line. `protocol_version` **must** match the daemon's
   (`VERSION`, currently **1**) or you get `accepted:false`. `mode` is `"lan"` or `"wan"`;
   `input_mode` is a hint (`"keyboard"` is the right generic choice for a custom jog client).
   ```json
   {"type":"hello","protocol_version":1,"mode":"lan","input_mode":"keyboard"}
   ```
   `token` is **required whenever the daemon has an agent token configured**
   (provisioned robots: `/etc/nori/agent.token`) — any mode, or the hello is rejected with
   `ack{accepted:false, error:"unauthorized"}`. Dev daemons without a token file skip the check.

2. **`ack`** (daemon → client) — `accepted:true` carries what you need to drive:
   `descriptor` (the exact `joints` / `base` / `aux` / `cameras` names and their `ranges`),
   `norm_mode` (units of every `.pos`), `watchdog_profile` (how long you can go silent), and
   `initial_state`. `accepted:false` carries an `error` and ends the session (e.g. version mismatch).

3. **Drive.** Stream `control` frames with a `jog` object — normalized rate in **`[-1, 1]`** per DOF.
   The daemon runs jog → IK → clamp → motor; **your values are always clamped robot-side.**
   ```json
   {"type":"control","jog":{"right_arm":{"shoulder_pan":0.3,"gripper":1.0},"right_lift":0.2}}
   ```
   Priority actions go as `command` (schema-canonical form uses `name`):
   ```json
   {"type":"command","name":"estop"}
   ```
   `reset_latch` clears a tripped E-STOP and requires a `token`.

4. **Telemetry** arrives continuously (~50 Hz): `state` (`<joint>.pos`, `x.vel`/`theta.vel`) plus
   per-motor `currents`. Read it on its own thread — don't block your send loop on it.

5. **`bye`** — `{"type":"bye"}` for a clean teardown so the daemon resets for the next client.

See `README.md` for the full message table and the **units** (lerobot-native: `.pos` normalized
`[-100,100]`, grippers `[0,100]`, base `theta.vel` in deg/s, `jog` axes `[-1,1]`).

## Safety model — why this is safe to hand out

**The daemon defends itself.** Clamping, the watchdog, E-STOP latching, rate limits, and the motor
torque lifecycle are all on the robot. **No sequence of protocol messages can drive the robot
outside its safe envelope.** Two consequences for your client:

- **Keep streaming.** If control frames stop arriving for longer than `ack.watchdog_profile.t_stop_ms`,
  the daemon safe-stops. To hold a pose, keep sending (a zero jog is fine); to move, send at ~50 Hz.
- **You can't "break" it from here.** The worst a malformed/hostile client does is get rejected or
  trigger a safe-stop — never an unsafe motion.

## Minimal client (Python, stdlib only)

```python
import socket, json, threading, time

ROBOT = ("robot.local", 7777)   # daemon host:port on the robot / your LAN

def reader(sock):
    """Print telemetry as it streams in."""
    buf = b""
    for chunk in iter(lambda: sock.recv(4096), b""):
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg["type"] == "telemetry":
                print("state:", msg.get("state", {}))
            elif msg["type"] == "error":
                print("error:", msg)

with socket.create_connection(ROBOT, timeout=5) as s:
    send = lambda obj: s.sendall((json.dumps(obj) + "\n").encode())

    # 1) Handshake — first line must be hello.
    send({"type": "hello", "protocol_version": 1, "mode": "lan", "input_mode": "keyboard"})

    # 2) Read the ack (first line back), then hand the socket to a reader thread.
    first = s.recv(65536)
    ack = json.loads(first.split(b"\n", 1)[0])
    assert ack["type"] == "ack" and ack.get("accepted"), ack
    print("connected. joints:", ack["descriptor"]["joints"])
    threading.Thread(target=reader, args=(s,), daemon=True).start()

    # 3) Drive at ~50 Hz for 2 s: pan the right shoulder. The daemon clamps everything.
    for _ in range(100):
        send({"type": "control", "jog": {"right_arm": {"shoulder_pan": 0.3}}})
        time.sleep(0.02)

    # 4) Leave cleanly.
    send({"type": "bye"})
```

Swap `shoulder_pan` for any name in `ack.descriptor.joints`, add `base`/`left_lift`/`right_lift`,
or send `{"type":"command","name":"estop"}` at any time.

## Camera frames over the LAN (companion channel)

The `:7777` control plane carries **no video** — by design (image bytes would stall control
frames). On the same LAN, raw per-camera frames are available on a **companion channel**: the
robot's capture bridge publishes every camera on its own ZeroMQ PUB socket.

**Availability:** only when the robot runs **multi-camera mode** (the bridge is the single
camera owner there; single-camera robots have no bridge) — and note the bridge is normally
**spawned when a teleop operator connects and stopped when they disconnect** (cameras capture
only during a session, to save power). So either keep an operator session open while you
consume frames, or ask the robot's operator to run the bridge standalone
(`--external-bridge` mode). Ask us for the robot's camera list if you can't read its
`cameras.json`.

**Ports:** `NORI_CAM_BASE_PORT + index` (base default **5555**), where `index` is the camera's
position in the robot's `cameras.json`. The camera *names* are the same roles the ack's
`descriptor.cameras` reports — e.g. with `["front","left_wrist","right_wrist"]`:
front→5555, left_wrist→5556, right_wrist→5557.

**Wire format:** one ZMQ message per frame —

```
b"<name> <capture_monotonic_seconds>\n" + <complete JPEG bytes>
```

Frames are the camera's **native MJPEG capture** (no re-encode, no compositing, no WebRTC);
resolution/rate are whatever the robot is configured to capture. Timestamps are the robot's
CLOCK_MONOTONIC — good for inter-frame timing, **not** wall-clock synced.

**Minimal consumer (Python + pyzmq):**

```python
import zmq

ctx = zmq.Context.instance()
sock = ctx.socket(zmq.SUB)
sock.setsockopt(zmq.SUBSCRIBE, b"")
sock.setsockopt(zmq.CONFLATE, 1)   # keep only the newest frame — do this
sock.setsockopt(zmq.RCVHWM, 1)
sock.connect("tcp://<ROBOT_IP>:5555")

while True:
    msg = sock.recv()                  # one message = one frame
    header, jpg = msg.split(b"\n", 1)  # b"<name> <ts>"
    name, ts = header.decode().split(" ", 1)
    # jpg is a complete JPEG, e.g.:
    # frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
```

**Semantics:** latest-wins on both ends (publisher and the `CONFLATE` subscriber above) — a
slow consumer drops frames rather than accumulating lag. Subscribe per camera; multiple
subscribers are fine (the robot's own compositor and recorder are two of them). Keep it to a
few subscribers — each one costs the robot a network write per frame.

**Security posture:** these sockets are **unauthenticated** and LAN-scoped — the same trusted-
network assumption as direct `:7777`. Fine on a dev bench; do not port-forward them. (The
robot operator can restrict the bind interface via `NORI_CAM_BIND`.)

**Stability:** this is an internal interface we're promoting for LAN development. The wire
format above is stable in practice and we'll flag any change here, but it is not yet under
`protocol_version` guarantees — check with us before building a product on it.

## Telemetry over the LAN (companion channel)

The daemon's `:7777` port serves **one client at a time** (single-writer safety model), so a
tool that only wants to *watch* joint state — a persistent 3D scene, a logger, a plotter —
shouldn't take that slot (and shouldn't need WebRTC). Instead: while a teleop session is
live, the robot's media bridge **re-publishes every frame the daemon sends** on one ZeroMQ
PUB socket. Read-only; nothing you send reaches the robot.

**Availability:** session-scoped, like the camera sockets above — the tap exists **only while
an operator session is connected** (the bridge process is per-session). No session, no
telemetry: the daemon itself isn't streaming either.

**Port:** `NORI_TELEM_PORT` (default **7788**); bind interface follows `NORI_CAM_BIND` unless
`NORI_TELEM_BIND` overrides it. The robot operator may have disabled it (`NORI_TELEM_PORT=0`).

**Wire format:** one ZMQ message per daemon frame — the **raw NDJSON line** exactly as the
daemon emitted it (no re-serialization, full ~50 Hz, *not* the ~15 Hz throttled copy the
WebRTC operator sees). Mostly `{"type":"telemetry",...}` with the same `state` / `currents`
fields described in step 4 above; `ack` / `error` / `action_status` frames ride through too —
filter on `type`. (`perception` frames do NOT appear here: they are a media-side sidecar
merged onto the WebRTC data channel, never emitted by the daemon on `:7777`.)

**Minimal consumer (Python + pyzmq):**

```python
import zmq, json

ctx = zmq.Context.instance()
sock = ctx.socket(zmq.SUB)
sock.setsockopt(zmq.SUBSCRIBE, b"")
sock.setsockopt(zmq.CONFLATE, 1)   # keep only the newest frame — do this
sock.connect("tcp://<ROBOT_IP>:7788")

while True:
    m = json.loads(sock.recv())
    if m.get("type") == "telemetry":
        print(m["state"])          # every "<joint>.pos" + base "x.vel"/"theta.vel"
```

**Semantics:** latest-wins — the publisher drops rather than queues for a slow subscriber,
and `CONFLATE=1` on your end keeps only the newest frame (skip it only if you're logging and
genuinely want every frame; then read fast). Multiple subscribers are fine.

**Security posture:** unauthenticated and LAN-scoped — identical to the camera sockets: fine
on a dev bench, never port-forward it. Same stability caveat as above: promoted-internal, not
yet under `protocol_version` guarantees.

## Remote / WAN

Direct `:7777` is a plaintext control socket for **trusted networks**. To drive a robot over the
internet, either:

- **Tunnel it** — SSH / WireGuard / a TLS front you control — and keep using the socket exactly as
  above; or
- **Use the WebRTC path** — the robot's `webrtc_robot.py` bridge exchanges the same control/telemetry
  frames over a WebRTC data channel (with STUN/TURN for NAT traversal and an out-of-band signaling
  rendezvous). The `@nori/sdk` TypeScript package speaks this end-to-end; re-implementing it in
  another language is more work than the tunnel option.

For dataset recording, read [Recording compatibility](README.md#recording-compatibility)
before choosing verbs. Review is an opt-in L3 extension: discover support before
starting, await correlated decisions by stable episode ID, and retain the
legacy L2/L3 distinctions for stop and discard.

## Reference

- `schema/*.json` — the canonical message definitions (Draft 2020-12). Validate against these.
- `fixtures/*.json` — a golden instance of each message; decode them in your tests.
- `python validate.py` — checks every fixture against its schema.
- `VERSION` — the integer `protocol_version` your `hello` must send.
