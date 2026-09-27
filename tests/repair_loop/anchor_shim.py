"""Track-1 anchor harness shim (scratch-only, never modifies runtime_work).

Operational model (same as the trust-anchor mission's batteryE shim):
* Each org gets a NESTED workdir <WORK_BASE>/<prefix>xxx/work so every org
  has its own isolated anchor journal (journal lives at
  <db_parent>/../anchor_store via default_anchor_paths).
* deploy_org(): boot a pristine org, then run the EXPLICIT deployment step
  (AnchorStore.initialize -- the same code anchor_admin.py init calls).
  Without this, ReviewBoard._store_verdict raises AnchorMissing.
* After every legitimate engine/harness write the journal tip is
  re-anchored (install_auto_anchor wraps OrgStore.insert and
  OracleRegistry._insert_chained). This is the operator legitimately
  anchoring its own writes. Attack writes use raw SQL through separate
  connections and bypass the wrapper -- still detected by chain audits
  and anchor verify.
* attach_org(): boot an existing workdir (journal exists; boot verifies)
  and install the wrapper. For fresh-process phases.

Reason for harness-anchored records is "verdict"; authority is
"track1:harness" to distinguish from engine-authored verdict anchors
(authority = remor:engine producer).
"""
import os
import sys
import tempfile

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

SHIM_DIR = os.path.dirname(os.path.abspath(__file__))
_WORK_ROOT = _os.environ.get("REMOR_TEST_WORK_ROOT", _os.path.join(SHIM_DIR, "work"))
WORK_BASE = _WORK_ROOT

from swarm_engine.agent_org.org import RemorOrganization  # noqa: E402
from swarm_engine.governance.anchor import (  # noqa: E402
    collect_anchor_heads,
    default_anchor_paths,
)


def fresh_workdir(prefix):
    """Create an isolated nested workdir; returns the org workdir path."""
    os.makedirs(WORK_BASE, exist_ok=True)
    base = tempfile.mkdtemp(prefix=prefix, dir=WORK_BASE)
    wd = os.path.join(base, "work")
    os.makedirs(wd, exist_ok=True)
    return wd


def anchor_paths_for(workdir):
    """The real default anchor paths for this org's DB (for evidence)."""
    return default_anchor_paths(os.path.join(workdir, "agent_org.db"))


def install_auto_anchor(org, authority=None, caller=None):
    """Re-anchor the journal tip after every legitimate store write.

    The org anchor is registry-bound: journal writes require an
    authenticated caller holding 'agent:anchor_write'. The harness acts
    as the engine operator (claimed authority = engine producer id).
    """
    from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID
    store, oreg = org.store, org.oregistry
    anchor = org.anchor
    if caller is None:
        caller = oreg.engine_handle()
    if authority is None:
        authority = ENGINE_PRODUCER_ID

    def _anchor_now():
        anchor.anchor(collect_anchor_heads(store, oreg),
                      reason="verdict", authority=authority, caller=caller)

    orig_insert = store.insert

    def insert_and_anchor(table, fields):
        seq = orig_insert(table, fields)
        _anchor_now()
        return seq

    store.insert = insert_and_anchor

    orig_chained = oreg._insert_chained

    def chained_and_anchor(table, fields):
        res = orig_chained(table, fields)
        _anchor_now()
        return res

    oreg._insert_chained = chained_and_anchor
    return org


def deploy_org(workdir, authority="track1:deploy"):
    """Boot a pristine org and run the explicit anchor deployment step."""
    org = RemorOrganization.boot(workdir)
    journal, key, _ = anchor_paths_for(workdir)
    from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID
    _eng = org.oregistry.engine_handle()
    digest = org.anchor.initialize(
        collect_anchor_heads(org.store, org.oregistry),
        ENGINE_PRODUCER_ID, caller=_eng)
    install_auto_anchor(org, authority=ENGINE_PRODUCER_ID, caller=_eng)
    print(f"ANCHOR-DEPLOY genesis={digest[:16]} journal={journal}",
          file=sys.stderr, flush=True)
    return org


def attach_org(workdir):
    """Boot an existing anchored org (fresh process); install wrapper."""
    org = RemorOrganization.boot(workdir)
    install_auto_anchor(org)
    journal, _, _ = anchor_paths_for(workdir)
    print(f"ANCHOR-ATTACH journal={journal} "
          f"records={org.anchor.record_count}",
          file=sys.stderr, flush=True)
    return org
