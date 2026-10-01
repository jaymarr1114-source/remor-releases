"""P9 — retirement signal: the coming-soon entry is now stale, and ONLY it.

Part A: drive every route-backed coming-soon entry EXCEPT recurring
through the REAL merged route table and assert each still returns its
exact typed 501 — proving this mission broke no other entry.

Part B: drive the recurring_disabled entry's probe (POST
/api/tasks/recurring, {}) and assert it does NOT return the typed
501 anymore — the route is real, so the probe gets the real handler's
honest validation response. That mismatch IS the retirement signal:
the inventory entry no longer describes the live runtime, and the
gate retires it (availability.py + test_coming_soon.py) at landing.

Part C: the full contract suite minus the route-backed test still
passes (inventory files untouched by this mission).
"""
import os
import shutil
import subprocess
import sys
import tempfile

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, _HERE  # noqa: E402

_WT = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_WT, "pylib"))

from swarm_engine.services.availability import build_coming_soon  # noqa: E402
from swarm_engine.services.contract_types import dispatch  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    build_services, close_services)

tmp = tempfile.mkdtemp(prefix="r1_p9_")
svc = build_services(tmp)
try:
    routes = svc["contract_routes"]
    listing = dispatch(routes, "GET", "/api/availability/coming-soon", {})
    check("listing ok", listing.get("ok") is True)
    entries = listing["coming_soon"]

    stale = []
    for e in entries:
        route = e["route"]
        if route is None:
            continue
        res = dispatch(routes, route["method"], route["path"],
                       dict(e.get("probe") or {}))
        un = res.get("unavailable") or {}
        if un.get("code") == e["unavailable"]["code"]:
            continue  # still honestly unavailable — untouched
        stale.append((e["kind"], e["unavailable"]["code"],
                      str(res)[:160]))

    check("exactly one stale entry: recurring_disabled",
          [c for _, c, _ in stale] == ["recurring_disabled"],
          str(stale))
    kind, code, detail = stale[0]
    check("stale entry is the recurring kind", kind == "recurring-schedules",
          kind)
    check("live route answers honestly (not 501)",
          "goal must be a non-empty string" in detail, detail)
finally:
    try:
        close_services(svc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# Part C: the rest of the contract suite is green (inventory untouched).
env = dict(os.environ)
env["PYTHONPATH"] = os.path.join(_WT, "pylib")
test_mod = "tests/contracts/test_coming_soon.py"
p = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", test_mod, "--deselect",
     f"{test_mod}::ComingSoonContractTest::test_route_backed_entries_match_live_handlers"],
    cwd=_WT, env=env, capture_output=True, text=True, timeout=300)
out = p.stdout + p.stderr
check("non-route coming-soon tests still pass",
      " failed" not in out and " error" not in out, out[-600:])

print("P9 PASS — recurring_disabled is the sole stale entry; gate-time "
      "retirement is safe")
