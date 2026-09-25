"""Method library for the demo domain: byte codecs.

Every method exposes the codec contract in generated modules:
    encode(data: bytes) -> bytes
    decode(blob: bytes) -> bytes        (exact inverse)
    selftest(data_hex: str) -> dict     ({"roundtrip_ok": bool, "ratio": float},
                                         never raises; ratio =
                                         len(encoded)/max(1, len(original)))

build(params) -> (code, entrypoint) with entrypoint ALWAYS "selftest".

Methods:
- chunk_dedup(chunk_size): fixed-size chunks with dictionary back-references,
  own framing (magic CDK1).
- rle(min_run): run-length encoding for runs >= min_run, own framing
  (magic RLE1).
- identity: framing-only baseline (magic IDN1).

All decode paths validate framing and raise ValueError on corrupt input;
selftest catches everything and reports roundtrip_ok=False instead of
raising (required: the IndependentValidator's novelty probes feed garbage
strings, and a raising selftest would misclassify every real codec as a
memorised table).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Tuple

IO_CONTRACT = {"input": "bytes", "output": "bytes"}

_SELFTEST_SRC = '''

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

_CHUNK_DEDUP_SRC = '''
_CHUNK_SIZE = @CHUNK_SIZE@
_MAGIC = b"CDK1"


def encode(data: bytes) -> bytes:
    """Fixed-size chunking with dictionary back-references."""
    data = bytes(data)
    chunks = [data[i:i + _CHUNK_SIZE]
              for i in range(0, len(data), _CHUNK_SIZE)]
    table = {}
    items = bytearray()
    for chunk in chunks:
        if chunk in table:
            items += b"\\x01" + table[chunk].to_bytes(2, "little")
        else:
            table[chunk] = len(table)
            items += b"\\x00" + len(chunk).to_bytes(2, "little") + chunk
    out = bytearray(_MAGIC)
    out += _CHUNK_SIZE.to_bytes(2, "little")
    out += len(chunks).to_bytes(4, "little")
    out += items
    return bytes(out)


def decode(blob: bytes) -> bytes:
    """Exact inverse of encode; raises ValueError on corrupt framing."""
    blob = bytes(blob)
    if blob[0:4] != _MAGIC:
        raise ValueError("chunk_dedup: bad magic")
    chunk_size = int.from_bytes(blob[4:6], "little")
    count = int.from_bytes(blob[6:10], "little")
    pos = 10
    table = []
    parts = []
    for _ in range(count):
        flag = blob[pos]
        pos += 1
        if flag == 0:
            size = int.from_bytes(blob[pos:pos + 2], "little")
            pos += 2
            chunk = bytes(blob[pos:pos + size])
            if len(chunk) != size:
                raise ValueError("chunk_dedup: truncated literal")
            pos += size
            table.append(chunk)
            parts.append(chunk)
        elif flag == 1:
            index = int.from_bytes(blob[pos:pos + 2], "little")
            pos += 2
            parts.append(table[index])
        else:
            raise ValueError("chunk_dedup: bad item flag")
    return b"".join(parts)
'''

_RLE_SRC = '''
_MIN_RUN = @MIN_RUN@
_MAGIC = b"RLE1"


def encode(data: bytes) -> bytes:
    """Run-length encode runs of >= _MIN_RUN identical bytes."""
    data = bytes(data)
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
            if len(literal) == 65535:
                items += b"\\x00" + len(literal).to_bytes(2, "little") \\
                    + bytes(literal)
                literal = bytearray()
                count += 1
            i += 1
    if literal:
        items += b"\\x00" + len(literal).to_bytes(2, "little") + bytes(literal)
        count += 1
    out = bytearray(_MAGIC)
    out += count.to_bytes(4, "little")
    out += items
    return bytes(out)


def decode(blob: bytes) -> bytes:
    """Exact inverse of encode; raises ValueError on corrupt framing."""
    blob = bytes(blob)
    if blob[0:4] != _MAGIC:
        raise ValueError("rle: bad magic")
    count = int.from_bytes(blob[4:8], "little")
    pos = 8
    parts = []
    for _ in range(count):
        flag = blob[pos]
        pos += 1
        if flag == 0:
            size = int.from_bytes(blob[pos:pos + 2], "little")
            pos += 2
            seg = bytes(blob[pos:pos + size])
            if len(seg) != size:
                raise ValueError("rle: truncated literal")
            pos += size
            parts.append(seg)
        elif flag == 1:
            value = blob[pos]
            run = int.from_bytes(blob[pos + 1:pos + 5], "little")
            pos += 5
            parts.append(bytes((value,)) * run)
        else:
            raise ValueError("rle: bad item flag")
    return b"".join(parts)
'''

_IDENTITY_SRC = '''
_MAGIC = b"IDN1"


def encode(data: bytes) -> bytes:
    """Framing-only baseline: no compression."""
    data = bytes(data)
    return _MAGIC + len(data).to_bytes(4, "little") + data


def decode(blob: bytes) -> bytes:
    """Exact inverse of encode; raises ValueError on corrupt framing."""
    blob = bytes(blob)
    if blob[0:4] != _MAGIC:
        raise ValueError("identity: bad magic")
    size = int.from_bytes(blob[4:8], "little")
    payload = bytes(blob[8:8 + size])
    if len(payload) != size:
        raise ValueError("identity: truncated payload")
    return payload
'''


def _header(technique: str, params: Dict) -> str:
    lines = [
        '"""Generated codec module (technique: %s, params: %s).' % (
            technique, params),
        "Contract: encode(data: bytes) -> bytes;",
        "decode(blob: bytes) -> bytes (exact inverse);",
        "selftest(data_hex: str) -> dict.",
        '"""',
    ]
    return "\n".join(lines) + "\n"


def build_chunk_dedup(params: Dict) -> Tuple[str, str]:
    chunk_size = int(params.get("chunk_size", 8))
    if not 1 <= chunk_size <= 65535:
        raise ValueError(f"chunk_size out of range: {chunk_size}")
    code = (_header("chunk_dedup", {"chunk_size": chunk_size})
            + _CHUNK_DEDUP_SRC.replace("@CHUNK_SIZE@", str(chunk_size))
            + _SELFTEST_SRC)
    compile(code, "<chunk_dedup>", "exec")
    return code, "selftest"


def build_rle(params: Dict) -> Tuple[str, str]:
    min_run = int(params.get("min_run", 4))
    if not 2 <= min_run <= 255:
        raise ValueError(f"min_run out of range: {min_run}")
    code = (_header("rle", {"min_run": min_run})
            + _RLE_SRC.replace("@MIN_RUN@", str(min_run))
            + _SELFTEST_SRC)
    compile(code, "<rle>", "exec")
    return code, "selftest"


def build_identity(params: Dict) -> Tuple[str, str]:
    if params:
        raise ValueError(f"identity takes no params, got {params}")
    code = _header("identity", {}) + _IDENTITY_SRC + _SELFTEST_SRC
    compile(code, "<identity>", "exec")
    return code, "selftest"


BuildFn = Callable[[Dict], Tuple[str, str]]

METHODS: Dict[str, Dict] = {
    "chunk_dedup": {
        "build": build_chunk_dedup,
        "param_grid": [{"chunk_size": 4}, {"chunk_size": 8},
                       {"chunk_size": 16}],
        "tags": ["chunk-repeat", "dictionary", "lossless"],
        "io_contract": dict(IO_CONTRACT),
    },
    "rle": {
        "build": build_rle,
        "param_grid": [{"min_run": 3}, {"min_run": 4}, {"min_run": 8}],
        "tags": ["byte-run", "run-length", "lossless"],
        "io_contract": dict(IO_CONTRACT),
    },
    "identity": {
        "build": build_identity,
        "param_grid": [{}],
        "tags": ["baseline", "lossless"],
        "io_contract": dict(IO_CONTRACT),
    },
}


# Deterministic probe corpus for the built-in round-trip checks.
_SELF_CHECK_VECTORS: List[bytes] = [
    b"",
    b"\x00",
    b"A",
    b"A" * 300,
    b"0123456789" * 30,
    bytes(range(256)),
    b"hello world " * 50,
    bytes((i * 37 + 11) % 256 for i in range(120)),
    b"\xde\xad\xbe\xef" * 40,
]


def self_check() -> Dict[str, int]:
    """Run every method x every param grid over the check vectors.

    Exact round-trip required; selftest must agree and never raise.
    Raises AssertionError on the first violation. Returns counts.
    """
    checked = 0
    for name, spec in METHODS.items():
        for params in spec["param_grid"]:
            code, entrypoint = spec["build"](params)
            assert entrypoint == "selftest", (name, entrypoint)
            ns: Dict = {}
            exec(compile(code, f"<{name}>", "exec"), ns)
            for fn_name in ("encode", "decode", "selftest"):
                assert callable(ns.get(fn_name)), (name, params, fn_name)
            for data in _SELF_CHECK_VECTORS:
                encoded = ns["encode"](data)
                assert isinstance(encoded, bytes), (name, params)
                decoded = ns["decode"](encoded)
                assert decoded == data, (name, params, data[:16])
                report = ns["selftest"](data.hex())
                assert isinstance(report, dict), (name, params)
                assert report.get("roundtrip_ok") is True, (name, params)
                assert isinstance(report.get("ratio"), float), (name, params)
            # selftest must not raise on garbage hex: truthful refusal.
            for bad in ("zz", "novel0probe", "0x123", "abc"):
                report = ns["selftest"](bad)
                assert report.get("roundtrip_ok") is False, (name, params, bad)
            checked += 1
    return {"method_params_checked": checked,
            "vectors_per_check": len(_SELF_CHECK_VECTORS)}
