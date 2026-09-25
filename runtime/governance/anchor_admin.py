"""Admin operations for the external trust anchor.

Operators never hand-edit the journal. The only legitimate way to move
anchored heads without a preceding anchored write is the audited
transition() path exposed here (migration / rollback / recovery); key
rotation is exposed too. Everything else is detected as tampering.

Usage (run with the repo's pylib on sys.path)::

    PYTHONPATH=<repo>/pylib python -m swarm_engine.governance.anchor_admin \
        --org-db /path/to/agent_org.db --oracle-db /path/to/oracle.db \
        init --authority ops:alice

    ... verify
    ... transition --reason recovery --authority ops:alice
    ... rotate-key --authority ops:alice

The anchor store is located via default_anchor_paths(org_db): the journal
and key live OUTSIDE the database directory, as siblings of it.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from swarm_engine.agent_org.store import OrgStore
from swarm_engine.governance.anchor import (
    AnchorStore,
    collect_anchor_heads,
    default_anchor_paths,
)
from swarm_engine.governance.oracle_binding import OracleRegistry


def open_context(org_db: str, oracle_db: str):
    """Open the two databases and their shared AnchorStore.

    Returns (org_store, oregistry, anchor, live_heads). Callers must
    close the stores when done.
    """
    store = OrgStore(org_db)
    oregistry = OracleRegistry(oracle_db)
    anchor = AnchorStore(*default_anchor_paths(os.path.abspath(org_db)))
    heads = collect_anchor_heads(store, oregistry)
    return store, oregistry, anchor, heads


def anchor_initialize(org_db: str, oracle_db: str, authority: str) -> str:
    """Deployment-time first-use capture: write the genesis record.

    Refuses if the journal already has records. Returns the genesis
    record_digest.
    """
    store, oregistry, anchor, heads = open_context(org_db, oracle_db)
    try:
        return anchor.initialize(heads, authority)
    finally:
        store.close()
        oregistry.close()


def anchor_verify(org_db: str, oracle_db: str):
    """Verify the journal against the live heads. Returns (ok, message)."""
    store, oregistry, anchor, heads = open_context(org_db, oracle_db)
    try:
        return anchor.verify(heads)
    finally:
        store.close()
        oregistry.close()


def anchor_transition(org_db: str, oracle_db: str, reason: str,
                      authority: str) -> str:
    """Admin-level audited head transition (migration/rollback/recovery).

    Appends a signed transition record committing the CURRENT live heads
    under the given reason and authority. This is the ONLY legitimate
    path for heads to change without a preceding anchored write -- e.g.
    after restoring a database from backup, or after a crash between a
    verdict-row insert and its anchor. Returns the record_digest.
    """
    store, oregistry, anchor, heads = open_context(org_db, oracle_db)
    try:
        return anchor.transition(heads, reason=reason, authority=authority)
    finally:
        store.close()
        oregistry.close()


def anchor_rotate_key(org_db: str, oracle_db: str,
                      authority: Optional[str] = None) -> str:
    """Rotate the anchor signing key. Returns the new key id."""
    store, oregistry, anchor, _heads = open_context(org_db, oracle_db)
    try:
        return anchor.rotate_key(authority=authority)
    finally:
        store.close()
        oregistry.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Admin operations for the REMOR external trust anchor. "
                    "Operators never hand-edit the journal.")
    parser.add_argument("--org-db", required=True,
                        help="path to agent_org.db")
    parser.add_argument("--oracle-db", required=True,
                        help="path to oracle.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init", help="deployment-time genesis (first-use capture)")
    p_init.add_argument("--authority", required=True,
                        help="operator id recorded on the genesis record")

    sub.add_parser("verify", help="verify the journal against live heads")

    p_tr = sub.add_parser(
        "transition",
        help="audited head change (migration/rollback/recovery)")
    p_tr.add_argument("--reason", required=True,
                      choices=["migration", "rollback", "recovery"],
                      help="audited reason for the head change")
    p_tr.add_argument("--authority", required=True,
                      help="operator id authorizing the transition")

    p_rot = sub.add_parser("rotate-key", help="rotate the anchor signing key")
    p_rot.add_argument("--authority", default=None,
                       help="operator id recorded on the rotation record")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            digest = anchor_initialize(args.org_db, args.oracle_db,
                                       args.authority)
            print(f"genesis written: {digest}")
        elif args.command == "verify":
            ok, msg = anchor_verify(args.org_db, args.oracle_db)
            print(msg)
            return 0 if ok else 1
        elif args.command == "transition":
            digest = anchor_transition(args.org_db, args.oracle_db,
                                       args.reason, args.authority)
            print(f"transition recorded ({args.reason}): {digest}")
        elif args.command == "rotate-key":
            new_id = anchor_rotate_key(args.org_db, args.oracle_db,
                                       args.authority)
            print(f"key rotated: new key_id {new_id}")
    except Exception as exc:  # admin errors fail closed with a message
        print(f"anchor_admin: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
