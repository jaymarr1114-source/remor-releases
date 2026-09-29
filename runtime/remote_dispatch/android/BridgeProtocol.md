# Dispatch Target Bridge Protocol v1

The bridge connects the Python `RemoteDispatchTarget` (enforcement point)
to the Android target app's `DispatchAccessibilityService` (input
substrate). It runs over a localhost TCP socket (`127.0.0.1:47631` by
default; `adb forward` for host-driven setups). JSON lines, UTF-8.

This protocol carries ONLY input primitives and indicator state. It is
NOT the dispatch wire protocol: sessions, consent, scope, replay
guards, TLS, and kill authority all live in the Python target agent
(`target_agent.py`), which is unchanged. The app never sees session
tokens and cannot grant consent.

## Framing

- Python -> app: `{"id": <int>, "cmd": <str>, "params": {…}}`
- App -> Python reply: `{"id": <int>, "ok": true, "result": {…}}`
  or `{"id": <int>, "ok": false, "error": "<reason>"}`
- App -> Python event (any time, same socket):
  `{"event": <str>, …fields}` — Python replies
  `{"event_ack": <str>, "ok": true|false, "error": …?}` on the same
  connection. (The Python reader thread dispatches events; requests
  and events interleave freely.)

## Commands (Python -> app)

| cmd | params | result | notes |
|---|---|---|---|
| `ping` | — | `{"service_connected": bool, "display": {"width": int, "height": int}}` | `service_connected` = AccessibilityService bound and ready |
| `tap` | `{"x": int, "y": int}` | `{"x": int, "y": int}` — the point actually tapped | single-finger tap via `dispatchGesture` |
| `swipe` | `{"x1","y1","x2","y2": int, "duration_ms": int}` | echo of the performed stroke | `duration_ms` clamped to 10..2000 |
| `scroll` | `{"x","y","dx","dy": int}` | `{"x","y","dx","dy"}` — what was scrolled | swipe from (x,y) opposite the delta; one swipe per command |
| `set_text` | `{"text": str ≤2000}` | `{"entered_chars": int}` | `ACTION_SET_TEXT` on the focused editable node; error if none focused |
| `global_action` | `{"action": "back"\|"home"\|"recents"\|"notifications"\|"quick_settings"\|"power_dialog"}` | `{}` | `performGlobalAction`; unknown name → error |
| `launch` | `{"package": str}` | `{"package": str, "launched": bool}` | launcher intent for the package; allowlist enforced by the Python side before this is ever sent |
| `indicator_show` | `{"session_id": str}` | `{}` | show the overlay banner for the session |
| `indicator_hide` | `{"session_id": str}` ("" = all) | `{}` | hide the overlay |
| `indicator_live` | — | `{"live": bool}` | is the overlay currently shown (real query, not cached) |
| `capture_frame` | `{"clip": {"x","y","w","h"}?, "max_w": int, "max_h": int}` | `{"width": int, "height": int, "format": "png", "data_b64": str, "ts": float, "synthesized": bool}` | screen capture for the session stream. `clip` (target pixels) is applied at capture; the app intersects it with the real display. `max_w`/`max_h` (0 = none) bound the returned size; the app downscales preserving aspect. `synthesized` is false for real captures. Fail-closed errors: `CAPTURE_UNAVAILABLE` (media-projection permission not granted — the app must not return a stale or placeholder image), `KILL_LATCHED` (the user kill latch gates capture like every actuator command: in the lost-event case the screen must not keep streaming after the user pressed KILL). The app never persists frames. |

Unknown `cmd` → `ok: false`. All failures carry an explicit `error`
string; the Python side raises, never fakes success.

## Events (app -> Python)

| event | fields | meaning |
|---|---|---|
| `user_kill` | `{"session_id": str, "user_token": str}` | the user pressed KILL on the target. Python calls `user_kill(session_id, user_token)` — the same target-local entry point as the bench — and replies `ok`/`error`. The controller has no path to this. |

## Fail-closed user kill latch

Pressing KILL sets a latch in the app (`killLatchedSession`) FIRST, before
any network I/O. While the latch is set, `BridgeServer` refuses every
actuator command (`tap`, `swipe`, `scroll`, `set_text`, `global_action`,
`launch`) with `{"ok": false, "error": "KILL_LATCHED: ..."}` — even if the
`user_kill` event never reached Python. `capture_frame` is gated by the
latch the same way: a latched kill must stop the screen stream even in
the lost-event case. Indicator commands bypass the latch (hide/show must
work during and after a kill; `indicator_live` is read-only), as do
control-plane commands, which travel the TLS dispatch channel rather
than the bridge.

## Screen capture (MediaProjection contract)

`capture_frame` is served by the app's MediaProjection flow, which the
app owns end to end:

1. The app requests the projection via `MediaProjectionManager
   .createScreenCaptureIntent()` and the user's one-time system grant
   (the standard media-projection consent dialog). No grant ->
   `capture_frame` returns `ok: false, error: "CAPTURE_UNAVAILABLE:
   media projection permission not granted"` -- fail-closed, never a
   placeholder image.
2. On grant, the app holds the `MediaProjection` and renders into an
   `ImageReader` surface sized to the current display.
3. Per `capture_frame`: `acquireLatestImage()`, intersect the requested
   `clip` with the real display, downscale to `max_w`/`max_h` keeping
   aspect, compress to PNG, base64, reply. Timestamps come from the
   image's acquisition time.
4. The app keeps no frame history and writes no frame to storage.

Until the app implements this flow, its `capture_frame` handler must
return `CAPTURE_UNAVAILABLE` (fail-closed). The bench harness
(`harness_bridge.py`) stands in with labeled synthesized frames to
prove the command path; real MediaProjection capture is unproven until
the app implements it against the real API.

The Python side maps the `KILL_LATCHED` token to `SubstrateRefusal`,
which the target agent converts to an explicit `action_refused`
(`TargetRefusal`) — never a transport error, never a fake ok. The refused
action is not charged to the session budget.

Rearm rule: only `indicator_show` for a DIFFERENT session clears the
latch. `indicator_show` is emitted by the Python target exactly once per
newly consented session, and consent is granted target-side — so rearming
requires a legitimate new locally approved session, never an arbitrary
remote command. An `indicator_show` for the latched session id itself is
a protocol violation and does NOT rearm.

## Security posture

- Localhost only: the app binds `127.0.0.1`. Remote reachability is the
  dispatch TLS channel's job, not the bridge's.
- The bridge grants input injection to whoever connects; on-device that
  is the Python target agent running under the same device owner. It
  must never be forwarded off-device except via `adb forward` by the
  device owner for bench testing.
- `launch` allowlist enforcement stays in Python (`Scope.allows` +
  `AndroidCursor.launch_app` re-check). The app performs no
  authorization of its own beyond executing well-formed commands.
