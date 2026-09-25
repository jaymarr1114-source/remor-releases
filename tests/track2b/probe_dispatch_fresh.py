import sys, os, json

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

# Original evidence DB from the source run (not portable; kept for provenance):
#   /home/hatch/workspace/remor_track3_learning/regression_track2b/fresh_s4cjnuba/eng.db
# Pass a real eng.db as argv[1] or via REMOR_PROBE_DB.
db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REMOR_PROBE_DB")
if not db_path or not os.path.isfile(db_path):
    print("SKIP probe_dispatch_fresh: needs a real eng.db via argv[1] or REMOR_PROBE_DB")
    raise SystemExit(2)
eng = SwarmEngine(db_path=db_path)
disp = NLToolDispatcher(eng, IntentRouter(eng))
hist = disp.history()
d2 = disp.dispatch('add two numbers together', {'a': 1, 'b': 2})
print(json.dumps({'n': len(hist), 'did': hist[0]['dispatch_id'] if hist else None,
                 'ok': d2.ok, 'result': d2.result}))
