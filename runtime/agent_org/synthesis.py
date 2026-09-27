"""Measured synthesis: from complementary experiences to a fused technique.

RelationshipFinder.find(exp_x, exp_y, runner):
  (a) contract compatibility: io_contract input/output "bytes", same
      problem_class on both experiences;
  (b) complementary tags: small tag overlap (intersection <= 1) with real
      difference (symmetric difference >= 2);
  (c) MEASURED behavioral complementarity on a fixed mixed probe corpus:
      run X alone, Y alone, and X-then-Y sequentially through the runner;
      require every probe to round-trip exactly AND
      ratio(X o Y) strictly < min(ratio(X), ratio(Y)).
      If X-then-Y fails the strict gate, Y-then-X is tried as a measured
      fallback; the order actually measured is recorded in the evidence.
  Returns a Relationship or None. Nothing is asserted without measurement.

OrgSynthesizer.synthesize(relationship, engine):
  Generates Z as FRESH code from the technique specs via the deterministic
  generator emit_fused_codec(x_spec, y_spec) -- built from spec fields
  (technique_name, params, io_contract, tags) ONLY, never from agent source
  text. The fused codec has its own framing (magic b"RMZ1"): a chunk table
  for deduplication plus an RLE stream for byte runs, with a single decode
  path. Gates:
  - token-level difflib ratio of Z vs X and Z vs Y each >= 0.40 (computed,
    not asserted), else SynthesisError;
  - sanity: Z round-trips the relationship corpus exactly and its measured
    aggregate ratio strictly beats both parents, else SynthesisError.
  Returns (code, entrypoint, relationship_evidence).
"""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.agent_org.exceptions import AuthorityError, SynthesisError

try:
    from swarm_engine.governance.oracle_binding import EngineOracleHandle
except Exception:  # pragma: no cover
    EngineOracleHandle = None  # type: ignore

_ENGINE = "remor:engine"

# Fixed, deterministic mixed probe corpus: byte-run-heavy AND
# chunk-repeat-heavy probes, plus structureless data. Fixed so the
# complementarity measurement is reproducible.
MIXED_CORPUS: List[str] = [
    "41" * 300,                                   # b"A"*300: byte runs
    "42" * 120 + "43" * 180,                      # two long runs
    "30313233343536373839" * 30,                  # b"0123456789"*30: chunks
    "deadbeef" * 40,                              # 4-byte pattern repeats
    "68656c6c6f20776f726c6420" * 50,              # b"hello world "*50
    "".join(f"{(i * 37 + 11) % 256:02x}" for i in range(120)),
    "00" * 64 + "0102030405060708" * 16,          # mixed
    "",                                           # empty edge
]


def tokenize(code: str) -> List[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[^\sA-Za-z0-9_]", code)


def token_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, tokenize(a),
                                   tokenize(b)).ratio()


@dataclass
class Relationship:
    x_exp_id: str
    y_exp_id: str
    x_technique: str
    y_technique: str
    order: str  # "x_then_y" | "y_then_x"
    evidence: Dict[str, Any] = field(default_factory=dict)


def _measure(code: str, entrypoint: str, runner: Any,
             corpus: List[str]) -> Tuple[bool, float, List[float]]:
    """Run code over the corpus via the runner. Returns
    (all_roundtrip_ok, aggregate_ratio, per_probe_ratios)."""
    report = runner(code, entrypoint, [{"data_hex": h} for h in corpus])
    if not report.ok:
        return False, float("inf"), []
    results = report.value
    if len(results) != len(corpus):
        return False, float("inf"), []
    total_in = 0
    total_out = 0
    ratios: List[float] = []
    for hex_str, res in zip(corpus, results):
        if not res.get("ok"):
            return False, float("inf"), []
        value = res.get("value") or {}
        if value.get("roundtrip_ok") is not True:
            return False, float("inf"), []
        data_len = len(bytes.fromhex(hex_str))
        ratio = float(value.get("ratio", float("inf")))
        ratios.append(ratio)
        total_in += data_len
        total_out += ratio * max(1, data_len)
    return True, (total_out / max(1, total_in)), ratios


def _namespace_component(code: str, prefix: str) -> Optional[str]:
    """Parse one codec component and rename its codec entry points.

    ``encode`` -> ``<prefix>_encode``, ``decode`` -> ``<prefix>_decode`` --
    both the ``FunctionDef`` names AND every ``Name`` reference (so
    recursive/self references keep working). Attribute accesses
    (``obj.encode``) are deliberately NOT renamed: they are ``Attribute``
    nodes, not ``Name`` nodes.

    Runs in the DRIVER (trusted parent process): ``ast`` parsing never
    executes the component. Returns the renamed source, or None when the
    component does not define both names (invalid composition).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "encode" not in defined or "decode" not in defined:
        return None

    class _Renamer(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            if node.name == "encode":
                node.name = prefix + "_encode"
            elif node.name == "decode":
                node.name = prefix + "_decode"
            return self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Name(self, node):
            if node.id == "encode":
                node.id = prefix + "_encode"
            elif node.id == "decode":
                node.id = prefix + "_decode"
            return node

    new_tree = _Renamer().visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


def _compose_code(x_code: str, y_code: str) -> str:
    """Build the X-then-Y sequential composition as a single PURE source.

    encode = Y.encode(X.encode(data)); decode = X.decode(Y.decode(blob)).

    W4-R1 (2026-09-27): the old wrapper embedded the untrusted component
    sources via ``exec`` + ``types.ModuleType`` -- unsound, because a
    TRUSTED profile would have handed the embedded code full builtins, and
    under PURE the wrapper itself needed the denied ``exec``/``types``.
    The rework namespaces the components with ``ast`` in the DRIVER
    (trusted parent process; parsing never executes): ``encode``/``decode``
    become ``x_encode``/``x_decode`` and ``y_encode``/``y_decode``, and the
    single composed source defines all four plus ``selftest``. The composed
    source contains no ``exec``, no ``types``, and no imports beyond what
    the (already PURE-measured) components import -- it runs clean under
    the PURE default.

    If either component does not define both names, the composition is
    invalid: a wrapper is returned whose ``selftest`` reports a failed
    measurement -- the same observable outcome as the old path, where the
    missing attribute raised at runtime inside the selftest's try.
    """
    x_src = _namespace_component(x_code, "x")
    y_src = _namespace_component(y_code, "y")
    if x_src is None or y_src is None:
        return (
            "def selftest(data_hex: str) -> dict:\n"
            "    return {\"roundtrip_ok\": False, \"ratio\": -1.0,\n"
            "            \"error\": \"invalid composition: component "
            "missing encode/decode\"}\n"
        )
    return (
        x_src + "\n\n" + y_src + "\n"
        "\n"
        "def encode(data: bytes) -> bytes:\n"
        "    return y_encode(x_encode(bytes(data)))\n"
        "\n"
        "def decode(blob: bytes) -> bytes:\n"
        "    return x_decode(y_decode(bytes(blob)))\n"
        "\n"
        "def selftest(data_hex: str) -> dict:\n"
        "    try:\n"
        "        data = bytes.fromhex(data_hex)\n"
        "    except Exception as exc:\n"
        "        return {\"roundtrip_ok\": False, \"ratio\": -1.0,\n"
        "                \"error\": (\"%s: %s\" % (type(exc).__name__, exc))[:160]}\n"
        "    try:\n"
        "        encoded = encode(data)\n"
        "        decoded = decode(encoded)\n"
        "    except Exception as exc:\n"
        "        return {\"roundtrip_ok\": False, \"ratio\": -1.0,\n"
        "                \"error\": (\"%s: %s\" % (type(exc).__name__, exc))[:160]}\n"
        "    return {\"roundtrip_ok\": bool(decoded == data),\n"
        "            \"ratio\": (len(encoded) / max(1, len(data)))}\n"
    )


class RelationshipFinder:
    def find(self, exp_x: Any, exp_y: Any, runner: Any) -> Optional[Relationship]:
        # (a) contract compatibility
        for exp in (exp_x, exp_y):
            io = exp.io_contract or {}
            if io.get("input") != "bytes" or io.get("output") != "bytes":
                return None
        if exp_x.problem_class != exp_y.problem_class:
            return None
        if exp_x.exp_id == exp_y.exp_id:
            return None
        # (b) complementary tags
        tx, ty = set(exp_x.tags or []), set(exp_y.tags or [])
        if len(tx & ty) > 1 or len(tx ^ ty) < 2:
            return None
        # (c) measured behavioral complementarity
        corpus = list(MIXED_CORPUS)
        ok_x, ratio_x, ratios_x = _measure(exp_x.code, exp_x.entrypoint,
                                          runner, corpus)
        ok_y, ratio_y, ratios_y = _measure(exp_y.code, exp_y.entrypoint,
                                          runner, corpus)
        if not (ok_x and ok_y):
            return None
        for order, first, second in (("x_then_y", exp_x, exp_y),
                                    ("y_then_x", exp_y, exp_x)):
            comp = _compose_code(first.code, second.code)
            ok_xy, ratio_xy, ratios_xy = _measure(comp, "selftest", runner,
                                                 corpus)
            if ok_xy and ratio_xy < min(ratio_x, ratio_y):
                evidence = {
                    "corpus_size": len(corpus),
                    "x_exp_id": exp_x.exp_id, "y_exp_id": exp_y.exp_id,
                    "x_technique": exp_x.technique_name,
                    "y_technique": exp_y.technique_name,
                    "x_ratio": ratio_x, "y_ratio": ratio_y,
                    "xy_ratio": ratio_xy,
                    "x_params": dict(exp_x.params or {}),
                    "y_params": dict(exp_y.params or {}),
                    "x_tags": list(exp_x.tags or []),
                    "y_tags": list(exp_y.tags or []),
                    "x_io_contract": dict(exp_x.io_contract or {}),
                    "y_io_contract": dict(exp_y.io_contract or {}),
                    "order": order,
                    "strictly_better": True,
                    "x_code": exp_x.code,
                    "y_code": exp_y.code,
                }
                return Relationship(
                    x_exp_id=exp_x.exp_id, y_exp_id=exp_y.exp_id,
                    x_technique=exp_x.technique_name,
                    y_technique=exp_y.technique_name,
                    order=order, evidence=evidence)
        return None


# ---------------------------------------------------------------------------
# Fused codec generator. Deterministic; built from spec fields only.
# ---------------------------------------------------------------------------

_FUSED_TEMPLATE = '''"""Fused codec (techniques fused: @TECH_LIST@).

One cohesive implementation with its own framing (magic RMZ1): the input
is first run-length encoded as a lean internal stream (the byte-run
technique); that stream is then split into chunks with dictionary
back-references over a chunk table (the chunk-repeat technique).
Single decode path: dedup-decode recovers the RLE stream, RLE-decode
recovers the data.
Contract: encode(data: bytes) -> bytes; decode(blob: bytes) -> bytes
(exact inverse); selftest(data_hex: str) -> dict (never raises).
"""
_CHUNK_SIZE = @CHUNK_SIZE@
_MIN_RUN = @MIN_RUN@
_MAGIC = b"RMZ1"


def _rle_encode(data: bytes) -> bytes:
    """Run-length encode whole input: the byte-run technique."""
    items = bytearray()
    literal = bytearray()
    count = 0
    i = 0
    n = len(data)
    while i < n:
        j = i + 1
        while j < n and data[j] == data[i]:
            j += 1
        run = j - i
        if run >= _MIN_RUN:
            if literal:
                items += b"\\x00" + len(literal).to_bytes(2, "little") \\
                    + bytes(literal)
                literal = bytearray()
                count += 1
            items += b"\\x01" + bytes((data[i],)) + run.to_bytes(4, "little")
            count += 1
            i = j
        else:
            literal.append(data[i])
            i += 1
    if literal:
        items += b"\\x00" + len(literal).to_bytes(2, "little") + bytes(literal)
        count += 1
    return count.to_bytes(4, "little") + bytes(items)


def _rle_decode(stream: bytes) -> bytes:
    """Exact inverse of _rle_encode; raises ValueError on corrupt input."""
    count = int.from_bytes(stream[0:4], "little")
    pos = 4
    parts = []
    for _ in range(count):
        flag = stream[pos]
        pos += 1
        if flag == 0:
            size = int.from_bytes(stream[pos:pos + 2], "little")
            pos += 2
            seg = bytes(stream[pos:pos + size])
            if len(seg) != size:
                raise ValueError("fused: truncated rle literal")
            pos += size
            parts.append(seg)
        elif flag == 1:
            value = stream[pos]
            run = int.from_bytes(stream[pos + 1:pos + 5], "little")
            pos += 5
            parts.append(bytes((value,)) * run)
        else:
            raise ValueError("fused: bad rle flag")
    return b"".join(parts)


def encode(data: bytes) -> bytes:
    """RLE pass, then chunk-level deduplication over the RLE stream."""
    data = bytes(data)
    stream = _rle_encode(data)
    chunks = [stream[i:i + _CHUNK_SIZE]
              for i in range(0, len(stream), _CHUNK_SIZE)]
    freq = {}
    for chunk in chunks:
        freq[chunk] = freq.get(chunk, 0) + 1
    table = []
    index = {}
    for chunk in chunks:
        if freq[chunk] >= 2 and chunk not in index:
            index[chunk] = len(table)
            table.append(chunk)
    items = bytearray()
    for chunk in chunks:
        if chunk in index:
            items += b"\\x02" + index[chunk].to_bytes(2, "little")
        else:
            items += b"\\x00" + len(chunk).to_bytes(2, "little") + chunk
    out = bytearray(_MAGIC)
    out += _CHUNK_SIZE.to_bytes(2, "little")
    out += len(table).to_bytes(4, "little")
    for entry in table:
        out += len(entry).to_bytes(2, "little") + entry
    out += len(chunks).to_bytes(4, "little")
    out += bytes(items)
    return bytes(out)


def decode(blob: bytes) -> bytes:
    """Exact inverse of encode; raises ValueError on corrupt framing."""
    blob = bytes(blob)
    if blob[0:4] != _MAGIC:
        raise ValueError("fused: bad magic")
    chunk_size = int.from_bytes(blob[4:6], "little")
    table_count = int.from_bytes(blob[6:10], "little")
    pos = 10
    table = []
    for _ in range(table_count):
        size = int.from_bytes(blob[pos:pos + 2], "little")
        pos += 2
        entry = bytes(blob[pos:pos + size])
        if len(entry) != size:
            raise ValueError("fused: truncated table entry")
        pos += size
        table.append(entry)
    item_count = int.from_bytes(blob[pos:pos + 4], "little")
    pos += 4
    parts = []
    for _ in range(item_count):
        flag = blob[pos]
        pos += 1
        if flag == 0:
            size = int.from_bytes(blob[pos:pos + 2], "little")
            pos += 2
            seg = bytes(blob[pos:pos + size])
            if len(seg) != size:
                raise ValueError("fused: truncated chunk literal")
            pos += size
            parts.append(seg)
        elif flag == 2:
            ref = int.from_bytes(blob[pos:pos + 2], "little")
            pos += 2
            parts.append(table[ref])
        else:
            raise ValueError("fused: bad item flag")
    return _rle_decode(b"".join(parts))


def selftest(data_hex: str) -> dict:
    """Contract probe: encode -> decode, report truthfully, never raise."""
    try:
        data = bytes.fromhex(data_hex)
    except Exception as exc:
        return {"roundtrip_ok": False, "ratio": -1.0,
                "error": ("%s: %s" % (type(exc).__name__, exc))[:160]}
    try:
        encoded = encode(data)
        decoded = decode(encoded)
    except Exception as exc:
        return {"roundtrip_ok": False, "ratio": -1.0,
                "error": ("%s: %s" % (type(exc).__name__, exc))[:160]}
    return {"roundtrip_ok": bool(decoded == data),
            "ratio": (len(encoded) / max(1, len(data)))}
'''


def _fused_params(x_spec: Dict[str, Any],
                  y_spec: Dict[str, Any]) -> Tuple[int, int]:
    """Derive fusion parameters from the specs' technique fields only."""
    chunk_size = 8
    min_run = 4
    for spec in (x_spec, y_spec):
        tech = spec.get("technique_name")
        params = spec.get("params") or {}
        if tech == "chunk_dedup" and "chunk_size" in params:
            chunk_size = int(params["chunk_size"])
        elif tech == "rle" and "min_run" in params:
            min_run = int(params["min_run"])
    if not 1 <= chunk_size <= 65535:
        raise SynthesisError(f"fused: chunk_size out of range: {chunk_size}")
    if not 2 <= min_run <= 255:
        raise SynthesisError(f"fused: min_run out of range: {min_run}")
    return chunk_size, min_run


def emit_fused_codec(x_spec: Dict[str, Any],
                     y_spec: Dict[str, Any]) -> str:
    """Deterministic fused-codec generator.

    Reads ONLY technique_name/params from the spec dicts (each must carry
    keys {technique_name, params, io_contract, tags}). Currently fuses the
    chunk_dedup + rle technique pair; any other pair raises SynthesisError
    rather than emitting a fake fusion.
    """
    for spec in (x_spec, y_spec):
        missing = [k for k in ("technique_name", "params", "io_contract",
                               "tags") if k not in spec]
        if missing:
            raise SynthesisError(
                f"emit_fused_codec: spec missing keys {missing}")
    techs = {x_spec["technique_name"], y_spec["technique_name"]}
    if techs != {"chunk_dedup", "rle"}:
        raise SynthesisError(
            f"emit_fused_codec: no fusion rule for technique pair "
            f"{sorted(techs)} (knows chunk_dedup + rle)")
    chunk_size, min_run = _fused_params(x_spec, y_spec)
    code = (_FUSED_TEMPLATE
            .replace("@TECH_LIST@", "+".join(sorted(techs)))
            .replace("@CHUNK_SIZE@", str(chunk_size))
            .replace("@MIN_RUN@", str(min_run)))
    compile(code, "<fused_codec>", "exec")
    return code


class OrgSynthesizer:
    def synthesize(self, relationship: Relationship,
                   engine: Any) -> Tuple[str, str, Dict[str, Any]]:
        """Generate Z from a measured relationship. Returns
        (code, entrypoint, relationship_evidence)."""
        if (EngineOracleHandle is None
                or not isinstance(engine, EngineOracleHandle)):
            raise AuthorityError(
                "synthesize requires a live EngineOracleHandle")
        if engine.producer_id != _ENGINE:
            raise AuthorityError(
                f"synthesize requires producer 'remor:engine', got "
                f"{engine.producer_id!r}")
        ev = relationship.evidence
        x_spec = {"technique_name": relationship.x_technique,
                  "params": dict(ev.get("x_params") or {}),
                  "io_contract": dict(ev.get("x_io_contract") or {}),
                  "tags": list(ev.get("x_tags") or [])}
        y_spec = {"technique_name": relationship.y_technique,
                  "params": dict(ev.get("y_params") or {}),
                  "io_contract": dict(ev.get("y_io_contract") or {}),
                  "tags": list(ev.get("y_tags") or [])}
        code = emit_fused_codec(x_spec, y_spec)

        # Distinctness gate: COMPUTED token-level similarity, not asserted.
        x_code = ev.get("x_code", "")
        y_code = ev.get("y_code", "")
        sim_x = token_similarity(code, x_code) if x_code else 0.0
        sim_y = token_similarity(code, y_code) if y_code else 0.0
        if sim_x < 0.40 or sim_y < 0.40:
            raise SynthesisError(
                f"distinctness gate refused: token similarity Z-vs-X "
                f"{sim_x:.3f}, Z-vs-Y {sim_y:.3f} (need >= 0.40 each)")

        # Sanity: Z must actually run, round-trip the relationship corpus
        # exactly, and beat both parents on aggregate ratio. (The driver's
        # independent verification is the admission gate; this only stops
        # us emitting code that is broken on its face.)
        ns: Dict[str, Any] = {}
        exec(compile(code, "<fused_sanity>", "exec"), ns)
        corpus = list(MIXED_CORPUS)
        total_in = 0
        total_out = 0
        for hex_str in corpus:
            report = ns["selftest"](hex_str)
            if report.get("roundtrip_ok") is not True:
                raise SynthesisError(
                    f"fused codec fails round-trip on corpus probe "
                    f"{hex_str[:32]!r}: refusing to emit")
            data_len = len(bytes.fromhex(hex_str))
            total_in += data_len
            total_out += float(report["ratio"]) * max(1, data_len)
        z_ratio = total_out / max(1, total_in)
        parent_best = min(ev["x_ratio"], ev["y_ratio"])
        if not z_ratio < parent_best:
            raise SynthesisError(
                f"fused codec ratio {z_ratio:.4f} does not beat parents "
                f"{parent_best:.4f}: refusing to emit")

        chunk_size, min_run = _fused_params(x_spec, y_spec)
        z_evidence = {
            "relationship": {
                "x_exp_id": relationship.x_exp_id,
                "y_exp_id": relationship.y_exp_id,
                "order": relationship.order,
                "x_ratio": ev.get("x_ratio"), "y_ratio": ev.get("y_ratio"),
                "xy_ratio": ev.get("xy_ratio"),
            },
            "z_technique": "fused",
            "z_params": {"chunk_size": chunk_size, "min_run": min_run},
            "z_ratio": z_ratio,
            "z_claimed_capabilities": ["codec:fused"],
            "distinctness": {"vs_x": sim_x, "vs_y": sim_y},
            "derived_from": [relationship.x_exp_id, relationship.y_exp_id],
        }
        return code, "selftest", z_evidence
