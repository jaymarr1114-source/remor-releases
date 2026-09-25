"""Discovery substrates: agents that find codecs by measurement.

Both substrates MEASURE every candidate they consider -- they execute the
generated codec code and score (exact_roundtrip, compression_ratio) on the
task's probe corpus. The winner is never hard-coded: it is the measured
argmin over the trials actually run.

- make_discovery_callable(): exhaustive grid search over codec_space.
- make_discovery_symbolic(): rules order candidates using workload features
  (dominant byte-run structure vs dominant chunk-repeat structure), then
  measure the ordered shortlist empirically and pick the measured winner.
  The rules influence ORDER, never the verdict.

Result dict contract (substrate boundary):
  implementation (code str), entrypoint ("selftest"), technique (str),
  params (dict), claimed_capabilities (list, e.g. ["codec:chunk_dedup"]),
  measurements (dict), notes (str).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from swarm_engine.agent_org.codec_space import METHODS
from swarm_engine.agent_org.substrates import (
    CallableSubstrate,
    Substrate,
    SymbolicSubstrate,
)


def measure_candidate(code: str, corpus: List[str]) -> Tuple[bool, float]:
    """Execute the codec and score it. Returns (all_roundtrip_ok, ratio).

    ratio = total_encoded_bytes / max(1, total_original_bytes).
    """
    ns: Dict[str, Any] = {}
    exec(compile(code, "<codec_candidate>", "exec"), ns)
    encode = ns["encode"]
    selftest = ns["selftest"]
    total_in = 0
    total_out = 0
    for hex_str in corpus:
        data = bytes.fromhex(hex_str)
        report = selftest(hex_str)
        if not isinstance(report, dict) or report.get("roundtrip_ok") is not True:
            return False, float("inf")
        total_in += len(data)
        total_out += len(encode(data))
    return True, (total_out / max(1, total_in))


def run_grid(order: List[str], corpus: List[str]) -> Tuple[Dict, Dict]:
    """Measure every param grid entry for the methods in `order`.

    Returns (trials, best) where best is the measured winner dict or None.
    """
    trials: Dict[str, Dict] = {}
    best: Dict[str, Any] = {}
    for name in order:
        spec = METHODS[name]
        for params in spec["param_grid"]:
            code, _entrypoint = spec["build"](params)
            ok, ratio = measure_candidate(code, corpus)
            key = f"{name}|{json.dumps(params, sort_keys=True)}"
            trials[key] = {"technique": name, "params": dict(params),
                           "roundtrip_ok": ok, "ratio": ratio}
            if ok and (not best or ratio < best["ratio"]):
                best = {"technique": name, "params": dict(params),
                        "code": code, "ratio": ratio}
    return trials, (best or None)


def discovery_result(order: List[str], corpus: List[str],
                     rule_note: str) -> Dict[str, Any]:
    trials, best = run_grid(order, corpus)
    if best is None:
        # Unreachable in practice: identity always round-trips. Fail closed
        # rather than inventing a winner.
        raise RuntimeError("discovery: no candidate round-tripped; refusing")
    spec = METHODS[best["technique"]]
    return {
        "implementation": best["code"],
        "entrypoint": "selftest",
        "technique": best["technique"],
        "params": best["params"],
        "tags": list(spec["tags"]),
        "io_contract": dict(spec["io_contract"]),
        "claimed_capabilities": [f"codec:{best['technique']}"],
        "measurements": {
            "trials": trials,
            "n_trials": len(trials),
            "corpus_probes": len(corpus),
            "candidate_order": list(order),
            "ordering_rule": rule_note,
            "winner_ratio": best["ratio"],
        },
        "notes": (f"measured {len(trials)} candidates on {len(corpus)} probes; "
                  f"winner {best['technique']}{best['params']} "
                  f"ratio={best['ratio']:.4f} selected by measurement"),
    }


def _probe_bytes(task: Dict[str, Any]) -> List[bytes]:
    corpus = task.get("probe_corpus")
    if not isinstance(corpus, list) or not corpus:
        raise ValueError("task['probe_corpus'] must be a non-empty list of hex strings")
    return [bytes.fromhex(h) for h in corpus]


def byte_run_density(task: Dict[str, Any], min_run: int = 4) -> bool:
    """True when a substantial fraction of bytes sit in long runs."""
    total = 0
    run_bytes = 0
    for data in _probe_bytes(task):
        total += len(data)
        i = 0
        n = len(data)
        while i < n:
            j = i + 1
            while j < n and data[j] == data[i]:
                j += 1
            if j - i >= min_run:
                run_bytes += j - i
            i = j
    return total > 0 and (run_bytes / total) > 0.20


def chunk_repeat_density(task: Dict[str, Any], chunk_size: int = 8) -> bool:
    """True when a substantial fraction of chunks are repeats."""
    total = 0
    dups = 0
    for data in _probe_bytes(task):
        chunks = [data[i:i + chunk_size]
                  for i in range(0, len(data), chunk_size)]
        seen = set()
        for chunk in chunks:
            total += 1
            if chunk in seen:
                dups += 1
            else:
                seen.add(chunk)
    return total > 0 and (dups / total) > 0.20


def make_discovery_callable() -> Substrate:
    def _fn(task: Dict[str, Any]) -> Dict[str, Any]:
        corpus = task.get("probe_corpus")
        if not isinstance(corpus, list) or not corpus:
            raise ValueError("task['probe_corpus'] required")
        return discovery_result(list(METHODS), list(corpus),
                                "exhaustive: no ordering rule, all candidates measured")
    return CallableSubstrate("discovery_grid", _fn, version="1")


def make_discovery_symbolic() -> Substrate:
    def _emit(order: List[str], note: str):
        def _go(task: Dict[str, Any]) -> Dict[str, Any]:
            corpus = task.get("probe_corpus")
            if not isinstance(corpus, list) or not corpus:
                raise ValueError("task['probe_corpus'] required")
            # Ordered shortlist: the first two ordered methods plus the
            # identity baseline; the winner is picked by MEASUREMENT.
            shortlist = list(order[:2])
            if "identity" not in shortlist:
                shortlist.append("identity")
            return discovery_result(shortlist, list(corpus), note)
        return _go

    rules = [
        {"when": lambda task: byte_run_density(task),
         "emit": _emit(["rle", "chunk_dedup", "identity"],
                       "dominant byte-run structure -> rle first")},
        {"when": lambda task: chunk_repeat_density(task),
         "emit": _emit(["chunk_dedup", "rle", "identity"],
                       "dominant chunk-repeat structure -> chunk_dedup first")},
        {"when": lambda task: True,
         "emit": _emit(["chunk_dedup", "rle", "identity"],
                       "no dominant feature -> default order")},
    ]
    return SymbolicSubstrate("discovery_rules", rules, version="1")
