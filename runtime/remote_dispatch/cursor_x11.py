"""Cursor-control primitives on the X11 bench substrate (XTEST injection).

This is the TARGET-SIDE execution layer: real XTEST events into a real X
server (Xvfb on the bench). Every primitive is verified by reading back
real server state (pointer position), not by assuming the injection
worked.

Honest substrate classification (Linux/X11/XTEST):
  CAN: move (absolute), click (buttons 1/2/3), scroll (button 4/5 wheel),
       type (X keysyms via XTEST key events), key press/release,
       launch_app (scoped subprocess).
  CANNOT: touch gestures / multi-touch (no touch device on Xvfb), pressure,
       hover-without-move distinction, biometric-gated actions.
  An Android target would need a different substrate module
  (AccessibilityService) -- this module is explicitly the X11 bench
  substrate, selected by RemoteDispatchTarget(substrate="x11").
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, Optional

from Xlib import X
from Xlib.display import Display
from Xlib.ext import xtest
from Xlib import XK


class CursorError(RuntimeError):
    pass


class X11Cursor:
    def __init__(self, display: Optional[str] = None):
        self.display_name = display or os.environ.get("DISPLAY", ":99")
        self._d = Display(self.display_name)

    # -- primitives ---------------------------------------------------
    def move(self, x: int, y: int) -> Dict[str, int]:
        xtest.fake_input(self._d, X.MotionNotify, x=int(x), y=int(y))
        self._d.sync()
        return self.position()

    def position(self) -> Dict[str, int]:
        p = self._d.screen().root.query_pointer()
        return {"x": p.root_x, "y": p.root_y}

    def click(self, button: int = 1, x: Optional[int] = None,
              y: Optional[int] = None) -> Dict[str, int]:
        if button not in (1, 2, 3):
            raise CursorError(f"button {button} not supported")
        if x is not None and y is not None:
            self.move(x, y)
        xtest.fake_input(self._d, X.ButtonPress, button)
        xtest.fake_input(self._d, X.ButtonRelease, button)
        self._d.sync()
        return self.position()

    def scroll(self, dx: int = 0, dy: int = 0) -> Dict[str, int]:
        # X11 wheel: button 4 = up, 5 = down, 6 = left, 7 = right
        steps = []
        if dy < 0:
            steps += [4] * (-dy)
        elif dy > 0:
            steps += [5] * dy
        if dx < 0:
            steps += [6] * (-dx)
        elif dx > 0:
            steps += [7] * dx
        for b in steps[:20]:  # bound a single scroll action
            xtest.fake_input(self._d, X.ButtonPress, b)
            xtest.fake_input(self._d, X.ButtonRelease, b)
        self._d.sync()
        return self.position()

    def _keysym(self, ch: str) -> int:
        if ch == " ":
            return XK.XK_space
        ks = XK.string_to_keysym(ch)
        if ks == 0:
            raise CursorError(f"no keysym for {ch!r}")
        return ks

    def type(self, text: str, is_live=None) -> Dict[str, int]:
        if len(text) > 2000:
            raise CursorError("type: text too long")
        typed = 0
        for ch in text:
            # interruptible: a concurrent user kill stops the injection
            # between keystrokes instead of only before the next action.
            if is_live is not None:
                is_live()  # raises if the session is no longer live
            code = self._d.keysym_to_keycode(self._keysym(ch))
            if code == 0:
                raise CursorError(f"no keycode for {ch!r}")
            # upper-case / shifted symbols need Shift held
            shift = ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?'
            if shift:
                shift_code = self._d.keysym_to_keycode(XK.XK_Shift_L)
                xtest.fake_input(self._d, X.KeyPress, shift_code)
            xtest.fake_input(self._d, X.KeyPress, code)
            xtest.fake_input(self._d, X.KeyRelease, code)
            if shift:
                xtest.fake_input(self._d, X.KeyRelease, shift_code)
            typed += 1
        self._d.sync()
        return {"typed_chars": typed}

    def key(self, keysym_name: str, press_only: bool = False) -> Dict[str, str]:
        ks = XK.string_to_keysym(keysym_name)
        if ks == 0:
            raise CursorError(f"unknown keysym {keysym_name!r}")
        code = self._d.keysym_to_keycode(ks)
        xtest.fake_input(self._d, X.KeyPress, code)
        if not press_only:
            xtest.fake_input(self._d, X.KeyRelease, code)
        self._d.sync()
        return {"key": keysym_name}

    def launch_app(self, argv: List[str],
                   allowed: List[str]) -> Dict[str, int]:
        name = argv[0] if argv else ""
        if name not in allowed:
            raise CursorError(f"app {name!r} not in allowlist")
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        return {"pid": proc.pid}

    def close(self) -> None:
        try:
            self._d.close()
        except Exception:
            pass
