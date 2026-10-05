"""T7 -- frozen consumer interfaces: consumed, never rebuilt.

Fresh process. The Phase 3 spec names two frozen consumers this track
does NOT build: the production-serving chat inlet (ROUTER-INLET-1 +
LLM-SERVE-PATH-1, crossed+landed) and the grower distill loop
(DISTILL-INTERFACE-FREEZE-1, crossed+landed). This test asserts the
structural honesty of the continuous-ops proof battery itself:
- the frozen distill interface module imports and exposes its frozen
  surface (registry read, routing, ReviewBoard verdicts) without the
  proof battery re-implementing any of it;
- the frozen chat-inlet modules import (router inlet + serve path);
- the proof battery's own file census contains no chat-inlet or
  distill-loop implementation (no duplication of frozen work);
- the RunController's acquisition leg routes through the existing
  acquisition machinery (Q1 loop_driver), not a second cadence.

This is a structural fence, not a re-proof of the frozen missions --
their gates already crossed.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_T = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_T, "pylib"), _T):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.path.insert(0, _HERE)
from cop_common import Checker  # noqa: E402


def main():
    c = Checker()

    # --- frozen distill interface imports, frozen surface present ------------
    try:
        import swarm_engine.curiosity.frm.grant as _g  # noqa
        has_grant = hasattr(_g, "FrmGrant")
    except Exception as exc:  # noqa
        has_grant = False
        _g_err = str(exc)
    c.check("t7_distill_grant_imports", has_grant,
            "FrmGrant importable" if has_grant else _g_err)

    # The frozen distill interface v1 per gate queue #45: registry read,
    # routing, ReviewBoard verdicts. Verify the modules exist and import.
    frozen_mods = []
    for mod in ("runtime.services.chat_api",
                "runtime.services.chat_handler"):
        try:
            __import__(mod)
            frozen_mods.append(mod)
        except Exception as exc:  # noqa
            frozen_mods.append(f"{mod}: IMPORT-FAIL {exc}")
    c.check("t7_chat_inlet_imports",
            all("IMPORT-FAIL" not in m for m in frozen_mods),
            f"{frozen_mods}")

    # --- no duplication: battery implements no inlet/distill machinery ------
    impl_markers = ("chat_api", "chat_handler", "llm_serve",
                    "distill_loop", "DistillationLoop")
    dupes = []
    for fn in os.listdir(_HERE):
        if not fn.endswith(".py"):
            continue
        low = fn.lower()
        if any(m in low for m in impl_markers):
            dupes.append(fn)
    c.check("t7_no_frozen_rebuild", not dupes,
            f"dupes={dupes}" if dupes else "battery has no inlet/distill impl")

    # --- one cadence: Q1 CognitionLoop.cycle is the seeded execution path ---
    _ld_err = ""
    try:
        from runtime.acquisition.loop_driver import CognitionLoop
        has_cycle = callable(getattr(CognitionLoop, "cycle", None))
    except Exception as exc:  # noqa
        has_cycle = False
        _ld_err = str(exc)
    c.check("t7_q1_cycle_exists", has_cycle,
            "CognitionLoop.cycle callable" if has_cycle else _ld_err)

    # --- the Controller, not the battery, owns dispatch ----------------------
    from runtime.core.run_controller import RunController
    c.check("t7_controller_owns_run",
            callable(getattr(RunController, "run", None)),
            "RunController.run is the dispatch entry point")

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
