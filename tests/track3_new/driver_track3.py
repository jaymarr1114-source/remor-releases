"""Track 3 end-to-end driver: dispatch -> organizational learning.

Proves the full frontier in a REAL deployment (no simulation):

  phase 1 (this process):
    1. boot full SwarmEngine + anchored RemorOrganization (fresh workdir)
    2. admit a real sort capability through the real AdmissionController
    3. create Agent A + governed assignment
    4. A maps a request text -> (dispatch_text, args) and performs a REAL
       dispatch: route -> capability selection -> validated args ->
       authorization -> Composer -> real result
    5. capture attributable dispatch evidence (agent+assignment bound)
    6. INDEPENDENT review: re-derive the result in a subprocess
    7. admit dispatch knowledge -> L2 organizational experience
       (technique generality verified on held-out cases first)
    8. run the A-I tampering battery + structural tests
    9. destroy Agent A (workspace gone, substrate gone, token gone)
  phase 2 (fresh process, same workdir):
    1. reattach org + engine (anchor verified on boot)
    2. create Agent B (new identity/workspace/substrate, no A-private state)
    3. ablation: B's held-out request is refused by the router
       (unknown_intent) -- without organizational knowledge B is stuck
    4. B retrieves ONLY organizational experience, executes the admitted
       technique, dispatches by the mapped parameters
    5. B's result is independently verified (evidence + review)
    6. causal checks: the experience (not hidden A state) enabled the work

Usage:
  python3 driver_track3.py --phase phase1 --workdir <dir>
  python3 driver_track3.py --phase phase2 --workdir <dir>

The workdir must be fresh for phase1. Phase2 reuses phase1's workdir in a
NEW process (fresh engine token, no A state).
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "pylib"))
# The dispatch-evidence re-execution harness runs in a subprocess via
# run_code, which inherits the environment: PYTHONPATH must let that
# subprocess import swarm_engine.
_PYL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pylib")
os.environ["PYTHONPATH"] = _PYL + os.pathsep + os.environ.get("PYTHONPATH", "")

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher  # noqa: E402
from swarm_engine.agent_org.org import RemorOrganization  # noqa: E402
from swarm_engine.agent_org.dispatch_learning import (  # noqa: E402
    capture_dispatch_evidence, get_evidence,
    DISPATCH_EVIDENCE_KIND,
)
from swarm_engine.agent_org.store import digest  # noqa: E402
from swarm_engine.acquisition.semantic import Case  # noqa: E402
from swarm_engine.governance.anchor import (  # noqa: E402
    AnchorStore, collect_anchor_heads, default_anchor_paths,
)
from swarm_engine.agent_org.exceptions import (  # noqa: E402
    VerificationFailed, AuthorityError,
)

PROBLEM_CLASS = "dispatch.arg_mapping.sort"

SORT_PLAN = {
    "name": "sort_numbers",
    "params": {"items": "list"},
    "steps": [{"id": "s1", "op": "sort",
               "args": {"items": {"$param": "items"}}}],
    "output": {"$step": "s1"},
}
SORT_GOAL = "sort numbers"

# Agent A's demonstrated request (the training instance -- NOT in the
# held-out generality set).
A_USER_TEXT = "sort the numbers 5 3 8 1"
A_EXPECTED_SORTED = [1, 3, 5, 8]

# Agent B's held-out request: materially related (same task structure --
# sort integers ascending) but not identical (new phrasing, new numbers).
# The router refuses it (unknown_intent); only organizational knowledge
# bridges the gap.
B_USER_TEXT = "arrange 12 4 8 in ascending order"
B_EXPECTED_SORTED = [4, 8, 12]

MAPPER_TEMPLATE = '''"""Dispatch technique: sort-request -> (dispatch_text, args).

Distilled from verified dispatch {evidence_id} (agent {agent_id}).
Verified on held-out request texts before admission; see the stored
generality verdict. Deterministic; no I/O. Refusals are VALUES
(ok=False), never silent -- mirroring the intent router's own
refusal-as-value policy.
"""
import re

CAPABILITY_ID = "{capability_id}"
CAPABILITY_VERSION = {capability_version}
CANONICAL_DISPATCH_TEXT = "sort numbers"

_SORT_PATTERNS = (
    r"\\bsort\\b",
    r"\\border\\b",
    r"\\barrange\\b",
    r"\\bascend(?:ing)?\\b",
    r"\\bsmallest\\b.*\\blargest\\b",
    r"\\blargest\\b.*\\bsmallest\\b",
)


def map_request(user_text):
    """Map a sort request to dispatch parameters, or refuse as a value."""
    if not isinstance(user_text, str):
        return {{"ok": False, "reason": "request must be text"}}
    low = user_text.lower()
    if not any(re.search(p, low) for p in _SORT_PATTERNS):
        return {{"ok": False, "reason": "not a sort request"}}
    nums = [int(x) for x in re.findall(r"-?\\d+", user_text)]
    if not nums:
        return {{"ok": False, "reason": "no integers found"}}
    return {{"ok": True,
             "dispatch_text": CANONICAL_DISPATCH_TEXT,
             "capability_id": CAPABILITY_ID,
             "capability_version": CAPABILITY_VERSION,
             "args": {{"items": nums}}}}
'''


def _mapper_ok(text, items):
    return {"ok": True, "dispatch_text": SORT_GOAL,
            "capability_id": CAPABILITY_ID_HOLDER, "args": {"items": items}}


# Filled after admission; the generality cases below reference it.
CAPABILITY_ID_HOLDER = "capability_id:filled-after-admission"


def generality_cases(capability_id, capability_version):
    """Held-out (text -> expected mapping) cases for the technique.

    Disjoint from A's demonstrated instance. Positives use new phrasings
    and new numbers; negatives require refusal-as-value.
    """
    def ok(text, items):
        return {"ok": True, "dispatch_text": SORT_GOAL,
                "capability_id": capability_id,
                "capability_version": capability_version,
                "args": {"items": items}}

    raw = [
        ("please sort 9 2 7", ok("please sort 9 2 7", [9, 2, 7])),
        ("arrange 4 1 9 in ascending order",
         ok("arrange 4 1 9 in ascending order", [4, 1, 9])),
        ("order these digits: 3 8 1", ok("order these digits: 3 8 1", [3, 8, 1])),
        ("put 6 2 5 in order from smallest to largest",
         ok("put 6 2 5 in order from smallest to largest", [6, 2, 5])),
        ("SORT 10 4", ok("SORT 10 4", [10, 4])),
        ("sort the values -3 0 12", ok("sort the values -3 0 12", [-3, 0, 12])),
        ("what is the weather today",
         {"ok": False, "reason": "not a sort request"}),
        ("sort", {"ok": False, "reason": "no integers found"}),
        ("delete the file", {"ok": False, "reason": "not a sort request"}),
        ("compute the average of 1 2 3",
         {"ok": False, "reason": "not a sort request"}),
    ]
    return [Case(args={"user_text": t}, expect=e, label=f"gen{i}")
            for i, (t, e) in enumerate(raw)]


# ---------------------------------------------------------------------------
# boot helpers
# ---------------------------------------------------------------------------

def fresh_workdir():
    base = tempfile.mkdtemp(prefix="track3_")
    wd = os.path.join(base, "work")
    os.makedirs(wd, exist_ok=True)
    return wd


def deploy_org(workdir, authority="track3:deploy"):
    import swarm_engine.agent_org.org as org_mod
    # Test-deployment choice: per-workdir anchor location so repeated test
    # deployments do not share the global default anchor journal. The
    # journal still lives OUTSIDE the database files themselves.
    anchor_dir = os.path.abspath(workdir) + "_anchor"
    os.makedirs(anchor_dir, exist_ok=True)

    def _per_workdir(db_path):
        stem = os.path.splitext(os.path.basename(db_path))[0] or "db"
        return (os.path.join(anchor_dir, stem + ".anchor.journal"),
                os.path.join(anchor_dir, stem + ".anchor.key"),
                os.path.abspath(db_path))

    org_mod.default_anchor_paths = _per_workdir
    org = RemorOrganization.boot(workdir)
    journal, key, _ = org_mod.default_anchor_paths(
        os.path.join(workdir, "agent_org.db"))
    # boot seeds templates and the __init__ runs before any anchor exists:
    # initialize the journal on the POST-BOOT state (genesis commits to
    # the seeded templates), then arm the auto-anchor.
    d = org.anchor.initialize(
        collect_anchor_heads(org.store, org.oregistry), authority)
    install_auto_anchor(org, authority="track3:harness")
    print(f"ANCHOR-DEPLOY genesis={d[:16]} journal={journal}", flush=True)
    return org


def attach_org(workdir):
    import swarm_engine.agent_org.org as org_mod
    anchor_dir = os.path.abspath(workdir) + "_anchor"

    def _per_workdir(db_path):
        stem = os.path.splitext(os.path.basename(db_path))[0] or "db"
        return (os.path.join(anchor_dir, stem + ".anchor.journal"),
                os.path.join(anchor_dir, stem + ".anchor.key"),
                os.path.abspath(db_path))

    org_mod.default_anchor_paths = _per_workdir
    org = RemorOrganization.boot(workdir)
    # boot() verified the journal above; re-arm the auto-anchor for this
    # process's writes.
    install_auto_anchor(org, authority="track3:harness")
    return org
    print(f"ANCHOR-ATTACH records={org.anchor.record_count}", flush=True)
    return org


def install_auto_anchor(org, authority="track3:harness"):
    store, oreg = org.store, org.oregistry
    anchor = org.anchor

    def _anchor_now():
        anchor.anchor(collect_anchor_heads(store, oreg),
                      reason="verdict", authority=authority)

    orig_insert = store.insert

    def insert_and_anchor(table, fields):
        seq = orig_insert(table, fields)
        _anchor_now()
        return seq

    store.insert = insert_and_anchor
    # The validation subprocess writes oracle rows (ob_authorizations,
    # ob_evaluations, ob_oracles) through OracleRegistry._insert_chained,
    # bypassing store.insert: wrap it too, or _store_verdict's anchor
    # check refuses on the subprocess's own writes.
    orig_ochain = oreg._insert_chained

    def ochain_and_anchor(table, fields):
        rid = orig_ochain(table, fields)
        _anchor_now()
        return rid

    oreg._insert_chained = ochain_and_anchor
    return org


def boot_engine(workdir):
    return SwarmEngine(db_path=os.path.join(workdir, "engine.db"))
