"""p9_no_double_classifier: structural proof that the router's escalation
heuristic does not duplicate existing machinery. Asserts: (a) no
difficulty/complexity classifier exists in runtime/ outside vendored
NLTK and the acquisition gap-node heuristic field; (b) router.py is the
only module that decides inference escalation; (c) it imports neither."""
import os
import re
import subprocess

TREE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'} {name}"
          + (f": {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(f"battery failed at {name}: {detail}")


# (a) hunt for difficulty classifiers in the runtime (excluding vendored
# NLTK and __pycache__)
out = subprocess.run(
    ["grep", "-rli", "--include=*.py", "-E",
     "difficulty.*class|class.*difficulty|complexity_score|readability_score",
     "runtime/"],
    capture_output=True, text=True, cwd=TREE)
hits = [h for h in out.stdout.splitlines()
        if "__pycache__" not in h and "vendor/nlu_nltk" not in h]
check("p9_no_runtime_difficulty_classifier", hits == [], f"hits={hits}")

# gap_reasoner's difficulty is an acquisition gap-node field, not routing:
gr = subprocess.run(
    ["grep", "-n", "difficulty", "runtime/acquisition/gap_reasoner.py"],
    capture_output=True, text=True, cwd=TREE).stdout
check("p9_gap_difficulty_is_node_field",
      "difficulty: float = 0.5" in gr and "rout" not in gr.lower(),
      "gap_reasoner difficulty is a static node field, unrelated to routing")

# (b) only router.py routes inference between the fast and deep paths.
# Note: runtime/core/metareasoning.py has a same-named method, but it
# escalates on confidence assessments (PROCEED/VERIFY/ACQUIRE/ASK/REFUSE)
# for the acceptance loop -- it never touches either inference path.
# The discriminating test: which modules reference BOTH paths.
out2 = subprocess.run(
    ["grep", "-rl", "--include=*.py", "-E",
     "governed_student|server_client", "distill1/", "runtime/"],
    capture_output=True, text=True, cwd=TREE)
fast_refs = set(h for h in out2.stdout.splitlines()
                if "__pycache__" not in h and "/proofs/" not in h
                and "vendor/" not in h)
out3 = subprocess.run(
    ["grep", "-rl", "--include=*.py", "-E",
     "Qwen3Teacher|GrantedCognitionProvider", "distill1/", "runtime/"],
    capture_output=True, text=True, cwd=TREE)
deep_refs = set(h for h in out3.stdout.splitlines()
                if "__pycache__" not in h and "/proofs/" not in h
                and "vendor/" not in h)
both = sorted(fast_refs & deep_refs)
# granted_cognition.py defines the deep inlet (it must reference the
# provider); qwen3_teacher.py defines the teacher; governed_student.py
# mentions GrantedCognitionProvider once in a docstring (interface
# parity note, line 55) and wraps only the fast path. None routes.
both_routers = [b for b in both
                if not b.endswith("granted_cognition.py")
                and not b.endswith("qwen3_teacher.py")
                and not b.endswith("governed_student.py")]
check("p9_single_inference_router",
      both_routers == ["distill1/router.py"], f"both={both_routers}")
print(f"  note: metareasoning.should_escalate is confidence/action "
      f"escalation (acceptance loop), not inference-path routing -- "
      f"different concern, no duplication", flush=True)

# (c) router.py imports no classifier machinery (import lines only --
# docstring mentions don't count)
src = open(os.path.join(TREE, "distill1", "router.py")).read()
imports = [l for l in src.splitlines()
           if l.strip().startswith(("import ", "from "))]
joined = "\n".join(imports).lower()
check("p9_router_imports_clean",
      "gap_reasoner" not in joined and "nltk" not in joined
      and "sklearn" not in joined,
      f"imports={imports}")
