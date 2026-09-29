"""Execution-target capability profiles for remote dispatch.

Standing architecture (James, 2026-09-28): the dispatch layer never
branches on device identity ("phone", "64-bit"). It declares
"capability X under constraints Y" against an abstract execution-target
capability profile; the substrate layer resolves the profile to the
actual implementation.

Resolution takes ONLY an ExecutionTargetProfile. The profile type
carries no device-identity fields (no device_id, no model name, no ABI
string), so there is nothing to branch on: the resolver keys on the
declared (input_mechanism, indicator_mechanism) capability pair.

Honest gap: profiles are currently DECLARED by the target at startup,
not probed against the real environment. James's sharpening (attested,
not just declared, or the abstraction can lie) is tracked future work;
until then the declaration is trusted the same way the pairing ceremony
trusts the presented agent identity.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple


# -- capability profile -------------------------------------------------
@dataclass(frozen=True)
class ExecutionTargetProfile:
    """What the target can do, not what the target is."""
    name: str
    input_mechanism: str      # e.g. "xtest" | "accessibility-bridge"
    indicator_mechanism: str  # e.g. "x11-window" | "overlay"
    hover_cursor: bool        # a visible pointer exists (move() moves it)
    multitouch: bool
    atomic_text: bool         # text injection is one atomic op (no
                              # per-keystroke interruption point)
    global_keys: bool         # back/home/recents style keys available
    app_launch: bool
    # Declared screen-capture mechanism, e.g. "xlib-image" |
    # "mediaprojection-bridge" | "none". Informational: substrate
    # resolution still keys on the (input, indicator) capability pair,
    # and Substrate.make_screen_capture fails closed when the resolved
    # substrate has no capture path. Same attestation caveat as the
    # rest of the profile (declared, not yet probed).
    capture_mechanism: str = "none"


X11_PROFILE = ExecutionTargetProfile(
    name="x11",
    input_mechanism="xtest",
    indicator_mechanism="x11-window",
    hover_cursor=True,
    multitouch=False,
    atomic_text=False,
    global_keys=True,
    app_launch=True,
    capture_mechanism="xlib-image",
)

ANDROID_PROFILE = ExecutionTargetProfile(
    name="android",
    input_mechanism="accessibility-bridge",
    indicator_mechanism="overlay",
    hover_cursor=False,
    multitouch=False,
    atomic_text=True,
    global_keys=True,
    app_launch=True,
    capture_mechanism="mediaprojection-bridge",
)


# -- substrate interfaces -----------------------------------------------
class CursorError(RuntimeError):
    """A real substrate failure. Propagates to the controller as an
    explicit error frame -- never converted into a fake ok."""


class SubstrateRefusal(CursorError):
    """The substrate refused an action as policy, not as failure.

    Raised when the substrate itself refuses -- e.g. the Android target
    app's local kill latch (KILL_LATCHED). The target agent converts this
    to an explicit action_refused (TargetRefusal); every other
    CursorError keeps the existing transport-error behavior, so the X11
    substrate's wire semantics are unchanged. Subclasses CursorError so
    existing handlers still catch it.
    """


class TargetCursor(ABC):
    """The six-primitive input interface every substrate implements."""

    @abstractmethod
    def move(self, x: int, y: int) -> Dict[str, Any]: ...
    @abstractmethod
    def click(self, button: int = 1, x: Optional[int] = None,
              y: Optional[int] = None) -> Dict[str, Any]: ...
    @abstractmethod
    def scroll(self, dx: int = 0, dy: int = 0) -> Dict[str, Any]: ...
    @abstractmethod
    def type(self, text: str, is_live=None) -> Dict[str, Any]: ...
    @abstractmethod
    def key(self, keysym: str) -> Dict[str, Any]: ...
    @abstractmethod
    def launch_app(self, argv: list, allowed: list) -> Dict[str, Any]: ...
    @abstractmethod
    def close(self) -> None: ...


class SubstrateIndicator(ABC):
    """The live-session indicator. live() queries the real display
    state; it is never a cached handle."""

    @abstractmethod
    def show(self, session_id: str) -> None: ...
    @abstractmethod
    def hide(self, session_id: Optional[str] = None) -> None: ...
    @abstractmethod
    def live(self) -> bool: ...


class ScreenCapture(ABC):
    """Target-side screen capture for the session stream.

    capture() returns a frame dict:
      {"width": int, "height": int, "format": "png",
       "data_b64": str,            # base64-encoded PNG bytes
       "ts": float,                # capture time (target clock)
       "synthesized": bool}        # True ONLY for harness stand-ins
    clip is {"x","y","w","h"} in target pixels, or None for the full
    display. Scope-clipping is applied by the TARGET AGENT before
    capture is called: the substrate clips to the given rect, never a
    full frame blurred after the fact.

    A frame that cannot be captured raises CursorError (fail-closed):
    the channel reports an explicit error frame, never a stale or
    fabricated image.
    """

    @abstractmethod
    def capture(self, clip: Optional[Dict[str, int]]) -> Dict[str, Any]: ...
    @abstractmethod
    def close(self) -> None: ...


class Substrate(ABC):
    profile: ExecutionTargetProfile

    @abstractmethod
    def make_cursor(self, **config) -> TargetCursor: ...
    @abstractmethod
    def make_indicator(self, **config) -> SubstrateIndicator: ...

    def make_screen_capture(self, **config) -> ScreenCapture:
        """Fail-closed default: a substrate with no capture path
        refuses the stream rather than faking frames."""
        raise CursorError(
            "screen capture unsupported on this substrate"
            f" ({self.profile.capture_mechanism!r}): the stream is"
            " unavailable, not silently degraded")

    def wire_target_events(self, target) -> None:
        """Optional: wire app/device->Python events (e.g. user-local
        kill) into the target's target-local entry points. Default is
        a no-op; substrates with an event channel override it."""


# -- X11 substrate (moved verbatim from target_agent.py) ------------------
class X11Substrate(Substrate):
    profile = X11_PROFILE

    def make_cursor(self, **config) -> TargetCursor:
        from .cursor_x11 import X11Cursor
        return X11Cursor(display=config.get("display"))

    def make_indicator(self, **config) -> SubstrateIndicator:
        return X11Indicator(display=config.get("display"),
                            on_failure=config.get("on_failure"))

    def make_screen_capture(self, **config) -> "ScreenCapture":
        from .cursor_x11 import X11ScreenCapture
        return X11ScreenCapture(display=config.get("display"))


INDICATOR_WM_NAME = "REMOR Remote Session LIVE"


class X11Indicator(SubstrateIndicator):
    """A mapped, override-redirect red banner named INDICATOR_WM_NAME
    on the target's own display, tracked per session. Verified by
    querying the X server (live()), not by a cached handle."""

    def __init__(self, display: Optional[str] = None,
                 on_failure: Optional[Callable] = None):
        self._display = display
        self._on_failure = on_failure
        self._indicators: Dict[str, Any] = {}

    def show(self, session_id: str) -> None:
        self.hide(session_id)
        try:
            from Xlib import X
            from Xlib.display import Display
            d = Display(self._display or os.environ.get("DISPLAY", ":99"))
            screen = d.screen()
            win = screen.root.create_window(
                screen.width_in_pixels - 360, 10, 340, 44, 0,
                screen.root_depth, X.InputOutput, X.CopyFromParent,
                override_redirect=True,
                background_pixel=screen.black_pixel)
            cmap = screen.default_colormap
            red = cmap.alloc_named_color("red").pixel
            win.change_attributes(background_pixel=red)
            win.set_wm_name(INDICATOR_WM_NAME)
            win.map()
            try:
                gc = win.create_gc(foreground=screen.white_pixel,
                                   background=red)
                font = d.open_font("fixed")
                gc.change(font=font.fid)
                win.draw_text(gc, 12, 28,
                              f"REMOTE SESSION LIVE {session_id[:13]}")
            except Exception:
                pass  # text is decoration; the window is the signal
            d.sync()
            self._indicators[session_id] = (win, d)
        except Exception:
            # indicator failure must never break the session; the session
            # itself is still governed. Record the miss visibly.
            if self._on_failure:
                self._on_failure("indicator_failed", session_id, {})

    def hide(self, session_id: Optional[str] = None) -> None:
        targets = ([session_id] if session_id
                   else list(self._indicators.keys()))
        for sid in targets:
            entry = self._indicators.pop(sid, None)
            if not entry:
                continue
            win, d = entry
            try:
                win.destroy()
                d.sync()
                d.close()
            except Exception:
                pass

    def live(self) -> bool:
        return self._find() is not None

    def _find(self):
        from Xlib.display import Display
        d = Display(self._display or os.environ.get("DISPLAY", ":99"))
        try:
            found = []

            def walk(w):
                for c in w.query_tree().children:
                    try:
                        if c.get_wm_name() == INDICATOR_WM_NAME:
                            found.append(c)
                    except Exception:
                        pass
                    walk(c)
            walk(d.screen().root)
            return found[0] if found else None
        finally:
            d.close()


# -- resolution (capability pair, never identity) -------------------------
def resolve_substrate(profile: ExecutionTargetProfile) -> Substrate:
    """Map a declared capability profile to its substrate.

    Keys on (input_mechanism, indicator_mechanism) -- what the target
    can do -- never on device identity. There is no device_id, model,
    or ABI parameter to branch on.
    """
    if not isinstance(profile, ExecutionTargetProfile):
        raise CursorError(
            "substrate resolution requires an ExecutionTargetProfile,"
            f" not {type(profile).__name__}")
    key: Tuple[str, str] = (profile.input_mechanism,
                            profile.indicator_mechanism)
    if key == (X11_PROFILE.input_mechanism,
               X11_PROFILE.indicator_mechanism):
        return X11Substrate()
    if key == (ANDROID_PROFILE.input_mechanism,
               ANDROID_PROFILE.indicator_mechanism):
        from . import cursor_android
        return cursor_android.AndroidSubstrate()
    raise CursorError(
        f"no substrate resolves capability pair {key!r}")
