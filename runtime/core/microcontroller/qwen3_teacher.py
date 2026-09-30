"""runtime/core/microcontroller/qwen3_teacher.py

QWEN3-ACQUIRE-1 — the first real teacher behind the GrantedCognitionProvider.

Qwen3Teacher implements the provider's teacher slot (the same interface
StubTeacher proves the governance with):

  * model_id / revision attributes -> provenance "borrowed:qwen3@<rev>"
  * estimate_cost_s(prompt) -> float, consulted BEFORE the borrow for the
    insufficient-grant deferral check
  * complete(prompt, context) -> (text, actual_seconds)

The teacher is PURE MECHANISM: it runs inference through an external
GGUF runtime (llama.cpp's llama-cli, acquired as a prebuilt binary --
external acquisition first, never hand-rolled inference). Every
governance property lives in GrantedCognitionProvider and is unchanged
by this swap: grant-gating, native-first, the FrmGrant-only isinstance
gate, charging through the substrate, telemetry, provenance.

License: the weights are Apache 2.0 (Qwen/Qwen3-8B-GGUF at pinned
revision 7c41481f57cb95916b40956ab2f0b139b296d974; license evidence in
proofs/qwen3_license/). This module contains no weights -- only the
wiring that shells out to the runtime. Do NOT fine-tune, redistribute,
or repackage the weights through this path.

Failure semantics: a transport failure (binary missing, timeout,
nonzero exit) propagates as an exception -- the provider treats it the
same as any teacher failure. The teacher never fabricates a result.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, Tuple

# Pinned HuggingFace revision of Qwen/Qwen3-8B-GGUF (license gate: GO,
# Apache 2.0 -- see proofs/qwen3_license/). The revision is part of the
# provenance string, so a weight change is a provenance change.
QWEN3_GGUF_REVISION = "7c41481f57cb95916b40956ab2f0b139b296d974"
QWEN3_GGUF_REPO = "Qwen/Qwen3-8B-GGUF"
QWEN3_GGUF_FILENAME = "Qwen3-8B-Q4_K_M.gguf"
QWEN3_GGUF_SHA256 = (
    "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785")

# Calibrated 2026-09-30 on this host (2-core VM, 7.9 GB RAM, llama.cpp
# b11284, Qwen3-8B-Q4_K_M, --single-turn --reasoning off): four real
# completions took 94-180 s wall (cold page cache ~180 s, warm ~94-141 s);
# llama.cpp reported "Generation: 1.0-2.5 t/s". The dominant cost is the
# 5 GB model page-in, not generation. The fixed overhead is set to the
# observed maximum so the pre-borrow deferral check errs toward deferring
# rather than overrunning a grant. Re-measure on a different execution
# target; do not trust this rate anywhere else.
_MEASURED_TOK_PER_S = 1.0
_FIXED_OVERHEAD_S = 180.0


def _extract_answer(stdout: str) -> str:
    """Strip llama-cli's chat chrome.

    In chat mode the CLI prints its banner, an "available commands" block,
    then echoes the prompt as a "> {prompt}" line; the model's completion
    is everything after that echo line. Without this the returned "text"
    would be CLI chrome, not the model's answer.
    """
    idx = stdout.rfind("\n> ")
    if idx == -1:
        return stdout.strip()
    tail = stdout[idx + 3:]
    nl = tail.find("\n")
    tail = tail[nl + 1:].strip() if nl != -1 else ""
    # Cut the CLI's timing footer and exit notice, which follow the
    # completion on stdout.
    for marker in ("\n[ Prompt:", "\nExiting..."):
        cut = tail.find(marker)
        if cut != -1:
            tail = tail[:cut]
    return tail.strip()


class Qwen3Teacher:
    """Real reasoning teacher: Qwen3-8B (Q4_K_M GGUF) via llama.cpp."""

    model_id = "qwen3"
    revision = QWEN3_GGUF_REVISION

    def __init__(self, *, gguf_path: str, llama_cli: str,
                 threads: int = 2, context_size: int = 512,
                 max_new_tokens: int = 64, timeout_s: float = 600.0) -> None:
        if not os.path.isfile(gguf_path):
            raise FileNotFoundError(f"Qwen3 GGUF not found: {gguf_path}")
        if not os.path.isfile(llama_cli) or not os.access(llama_cli, os.X_OK):
            raise FileNotFoundError(f"llama-cli not found/executable: {llama_cli}")
        self._gguf = gguf_path
        self._cli = llama_cli
        self._threads = threads
        self._ctx = context_size
        self._max_new = max_new_tokens
        self._timeout = timeout_s

    # -- teacher slot ----------------------------------------------------

    def estimate_cost_s(self, prompt: str) -> float:
        """Conservative cost estimate for the pre-borrow deferral check.

        tokens ~= prompt chars / 4 + max_new_tokens, at the measured
        generation rate, plus a fixed overhead margin. Documented and
        deterministic; deliberately pessimistic.
        """
        est_tokens = len(prompt or "") / 4.0 + self._max_new
        return _FIXED_OVERHEAD_S + est_tokens / _MEASURED_TOK_PER_S

    def complete(self, prompt: str,
                 context: Dict[str, Any]) -> Tuple[str, float]:
        """Run one completion through llama-cli. Returns (text, seconds).

        Transport failures propagate -- never a fabricated result.
        """
        parts = []
        refusal = (context or {}).get("native_refusal")
        if refusal:
            parts.append(f"[native refusal that justifies this borrow: {refusal}]")
        parts.append(prompt or "")
        full_prompt = "\n".join(p for p in parts if p)

        # llama.cpp b11284's llama-cli defaults to an interactive chat loop;
        # --single-turn forces one-shot mode, --reasoning off skips Qwen3's
        # thinking trace, and stdin is closed so it can never block waiting
        # for a user. Verified 2026-09-30: without --single-turn the
        # process sat waiting on stdin for 10+ minutes doing no inference.
        cmd = [self._cli,
               "-m", self._gguf,
               "-p", full_prompt,
               "-n", str(self._max_new),
               "-c", str(self._ctx),
               "-t", str(self._threads),
               "--no-display-prompt",
               "--temp", "0.2",
               "--log-disable",
               "--single-turn",
               "--reasoning", "off"]
        start = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=self._timeout,
                              stdin=subprocess.DEVNULL)
        actual_s = max(time.monotonic() - start, 0.001)
        if proc.returncode != 0:
            raise RuntimeError(
                f"llama-cli failed (exit {proc.returncode}): "
                f"{proc.stderr[-500:]}")
        text = _extract_answer(proc.stdout)
        if not text:
            raise RuntimeError(
                f"llama-cli returned no completion text; stdout tail: "
                f"{proc.stdout[-300:]}")
        return text, actual_s
