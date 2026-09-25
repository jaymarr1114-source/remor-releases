import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pylib"))
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

db = tempfile.mkdtemp(prefix="probe_route_") + "/e.db"
eng = SwarmEngine(db_path=db)
plan = {'name':'sort_numbers','params':{'items':'list'},
        'steps':[{'id':'s1','op':'sort','args':{'items':{'$param':'items'}}}],
        'output':{'$step':'s1'}}
res = eng.admission.admit(goal="sort numbers", plan=plan, name="sort_numbers")
print("admit:", res.ok, getattr(res,'capability_id',None), getattr(res,'reasons',None) or getattr(res,'reason',None))
if not res.ok:
    raise SystemExit(1)
cap_id = res.capability_id
disp = NLToolDispatcher(eng)
texts = [
    "sort the numbers 5 3 8 1",
    "sort numbers",
    "please sort 9 2 7",
    "arrange 4 1 9 in ascending order",
    "order these digits: 3 8 1",
    "put 6 2 5 in order from smallest to largest",
]
for t in texts:
    r = disp.router.route(t)
    print(repr(t), "->", r.ok, r.via, round(r.score,3), r.refusal)
# full dispatch of A-text
d = disp.dispatch("sort the numbers 5 3 8 1", {"items":[5,3,8,1]}, producer="probe")
print("dispatch:", d.ok, d.value, d.dispatch_id, d.error)
