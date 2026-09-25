import sys, os
sys.path.insert(0, os.path.join(r'/home/hatch/workspace/remor_convergence/canonical/tests/track2b', '..', 'pylib'))
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.integrity import effective_status
eng = SwarmEngine(db_path=r'/home/hatch/workspace/remor_convergence/canonical/tests/track2b/restore_causal_0qdayulw/eng.db')
eff = effective_status(eng, r'cap_bca1a23957fcf8aeb495')
prim = eng.primitives.get('acquired.cap_bca1a23957fcf8aeb495')
val = prim.fn(a=7, b=8) if prim else None
print('EFF:' + eff['effective'] + ' VAL:' + str(val))
