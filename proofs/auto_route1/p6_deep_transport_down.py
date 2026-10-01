"""p6_deep_transport_down: the deep teacher points at a corrupt GGUF.
llama-cli fails honestly; the router catches it, reports the real
transport error, charges zero for deep; the fast answer stands."""
import os
import tempfile
from common import (make_router, make_grant, check, Qwen3Teacher, GGUF,
                    LLAMA_CLI)

fd, bad_gguf = tempfile.mkstemp(suffix=".gguf")
os.write(fd, b"not a model file")
os.close(fd)
try:
    bad_teacher = Qwen3Teacher(gguf_path=bad_gguf, llama_cli=LLAMA_CLI)
    router, substrate = make_router(teacher=bad_teacher)
    sg = make_grant(budget_s=400.0)
    dg = make_grant(budget_s=2000.0)

    out = router.chat("Think hard: why is the sky blue?",
                      student_grant=sg, deep_grant=dg, think_hard=True)

    check("p6_fast_ok", out["fast"]["ok"] is True)
    check("p6_deep_attempted", out["deep"]["attempted"] is True)
    check("p6_deep_failed_honestly",
          out["deep"].get("ok") is False
          and "deep_failed:transport" in out["deep"]["error"],
          f"error={out['deep'].get('error')}")
    check("p6_zero_deep_charge",
          out["deep"].get("charged_s", 0.0) == 0.0
          and router._deep.grant_consumed_s(dg.grant_id) == 0.0)
    check("p6_served_falls_back_to_fast",
          out["served"]["via"] == "fast"
          and out["served"]["materially_better"] is False)
finally:
    os.unlink(bad_gguf)
