"""PLOOP-11 kill-resume driver: process A dies mid-handoff, process B
resumes from disk in a fresh process, then a replay attempt must be
refused loudly. Runs the two processes SEQUENTIALLY (never overlapping
the main proof battery or any sibling battery).
"""
import json
import os
import subprocess
import sys
import tempfile

WT = os.environ.get(
    "PLOOP11_WT", os.path.expanduser("~/workspace/remor_convergence/"
                                     "worktrees/ploop11"))
PY = sys.executable
ENV = dict(os.environ, PLOOP11_WT=WT)


def run(args):
    return subprocess.run(args, capture_output=True, text=True, env=ENV,
                          timeout=600)


def main():
    workdir = tempfile.mkdtemp(prefix="ploop11_killresume_")
    result = os.path.join(workdir, "handoff_identity.json")
    verdict = os.path.join(workdir, "verdict.json")
    fails = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}"
              + (f" -- {str(detail)[:160]}" if detail and not cond else ""),
              flush=True)
        if not cond:
            fails.append(name)

    # A dies mid-handoff
    a = run([PY, os.path.join(WT, "proofs", "ploop11_proc_a.py"),
             workdir, result])
    check("KR1: process A produced and died mid-handoff (exit 0)",
          a.returncode == 0, a.stderr[-400:] if a.returncode else a.stdout)
    check("KR2: durable handoff identity written",
          os.path.exists(result))
    identity = json.load(open(result)) if os.path.exists(result) else {}
    check("KR3: checkpoint id is durable",
          bool(identity.get("checkpoint_id")), str(identity.get(
              "checkpoint_id")))

    # B resumes in a fresh process
    b = run([PY, os.path.join(WT, "proofs", "ploop11_proc_b.py"),
             workdir, result, verdict])
    check("KR4: process B resumed from disk (exit 0)",
          b.returncode == 0,
          (b.stderr[-400:] if b.returncode else b.stdout))
    check("KR5: verdict written with unbroken lineage",
          os.path.exists(verdict))

    # Replay: B again must refuse loudly (checkpoint consumed)
    b2 = run([PY, os.path.join(WT, "proofs", "ploop11_proc_b.py"),
              workdir, result, verdict + ".replay"])
    check("KR6: replay of the consumed handoff refused (exit nonzero)",
          b2.returncode != 0, b2.stdout[-200:] + b2.stderr[-200:])
    check("KR7: replay refusal names the cause",
          "consumed" in (b2.stdout + b2.stderr).lower()
          or "replay" in (b2.stdout + b2.stderr).lower(),
          (b2.stdout + b2.stderr)[-200:])

    print(f"\nPLOOP-11 kill-resume: {7-len(fails)}/7 PASS, "
          f"{len(fails)} FAIL", flush=True)
    sys.exit(1 if fails else 0)


main()
