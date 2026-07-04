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
  ── hello ──────────────►        first line MUST be a hello (schema/hello.json)
        ◄──────── ack ──          accepted:true + descriptor, or accepted:false + error (schema/ack.json)
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
   For `mode:"wan"` a scoped `token` is **required** (the daemon rejects WAN without one).

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

## Remote / WAN

Direct `:7777` is a plaintext control socket for **trusted networks**. To drive a robot over the
internet, either:

- **Tunnel it** — SSH / WireGuard / a TLS front you control — and keep using the socket exactly as
  above; or
- **Use the WebRTC path** — the robot's `webrtc_robot.py` bridge exchanges the same control/telemetry
  frames over a WebRTC data channel (with STUN/TURN for NAT traversal and an out-of-band signaling
  rendezvous). The `@nori/sdk` TypeScript package speaks this end-to-end; re-implementing it in
  another language is more work than the tunnel option.

## Reference

- `schema/*.json` — the canonical message definitions (Draft 2020-12). Validate against these.
- `fixtures/*.json` — a golden instance of each message; decode them in your tests.
- `python validate.py` — checks every fixture against its schema.
- `VERSION` — the integer `protocol_version` your `hello` must send.
