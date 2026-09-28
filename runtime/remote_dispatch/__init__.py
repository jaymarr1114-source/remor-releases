"""Remote dispatch: REMOR driving a target device's cursor through a
governed session.

James's definition (2026-09-27): remote dispatch = REMOR remotely
assuming control over the cursor of the device.

Architecture:
  session_model.py -- devices, sessions, consent, scope, chained events
  protocol.py      -- framed JSON wire protocol with replay guards
  tls.py           -- TLS with pinned target identity (no CA; the pin
                      bound at pairing IS the authentication)
  channel.py       -- TLS transport, mutual-auth handshake
  cursor_x11.py    -- target-side X11/XTEST cursor primitives (bench)
  target_agent.py  -- target-side enforcement point (never trusts the
                      controller; consent/kill are target-local)
  controller.py    -- controller side: pair, request, connect, drive

Trust: built on governance/caller_authorization.py (agent identities,
decision classes, default-deny, chained revocation). Consent is granted
only by the user principal on the target; the controller has no path to
grant it. Scope and kill are enforced by the target per action.
"""
