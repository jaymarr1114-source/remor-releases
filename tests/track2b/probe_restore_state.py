import sys, os

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.integrity import effective_status

# Original evidence DB from the source run (not portable; kept for provenance):
#   /home/hatch/workspace/remor_track3_learning/regression_track2b/restore_causal__9hnulb7/eng.db
# Pass a real eng.db as argv[1] or via REMOR_PROBE_DB.
db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REMOR_PROBE_DB")
if not db_path or not os.path.isfile(db_path):
    print("SKIP probe_restore_state: needs a real eng.db via argv[1] or REMOR_PROBE_DB")
    raise SystemExit(2)
eng = SwarmEngine(db_path=db_path)
cap_id = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("REMOR_PROBE_CAP", "cap_bca1a23957fcf8aeb495")
eff = effective_status(eng, cap_id)
prim = eng.primitives.get('acquired.%s' % cap_id)
val = prim.fn(a=7, b=8) if prim else None
print('EFF:' + eff['effective'] + ' VAL:' + str(val))
