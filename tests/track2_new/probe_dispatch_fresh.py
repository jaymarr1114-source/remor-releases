import sys, os, json
sys.path.insert(0, os.path.join(r'/home/hatch/workspace/remor_convergence/canonical/tests/track2_new', '..', '..', 'pylib'))
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher
eng = SwarmEngine(db_path=r'/home/hatch/workspace/remor_convergence/canonical/tests/track2_new/fresh_3x29ar4n/eng.db')
disp = NLToolDispatcher(eng, IntentRouter(eng))
hist = disp.history()
d2 = disp.dispatch('add two numbers together', {'a': 1, 'b': 2})
print(json.dumps({'n': len(hist), 'did': hist[0]['dispatch_id'] if hist else None,
                 'ok': d2.ok, 'result': d2.result}))
