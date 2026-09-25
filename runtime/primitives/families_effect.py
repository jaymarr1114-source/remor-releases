"""
swarm_engine/primitives/families_effect.py

The governed half of the vocabulary. Every primitive here declares a non-PURE
Effect, so the registry routes each call through the Governor before the
function body ever runs. A synthesized capability that reaches for the network
without a NETWORK grant fails at the gate, not halfway through a side effect.

Families in this module:
    filesystem   network    knowledge   observation
    config       caching    concurrency debugging
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from swarm_engine.primitives.core import (
    ANY, BOOL, BYTES, CALLABLE, DICT, FLOAT, INT, LIST, NUM, OPT, STR, TUPLE,
    UNION, Effect, ExecContext, Primitive, PrimitiveRegistry, infer,
)


def _p(reg, name, family, fn, inputs, output, effects=(Effect.PURE,),
       needs_ctx=False, doc=""):
    reg.register(Primitive(name=name, family=family, fn=fn, inputs=inputs,
                           output=output, effects=effects, needs_ctx=needs_ctx,
                           doc=doc), overwrite=True)


# ===========================================================================
# 9. FILESYSTEM
# ===========================================================================

def register_filesystem(reg: PrimitiveRegistry) -> None:
    F = "filesystem"
    R, W = (Effect.READ_FS,), (Effect.WRITE_FS,)

    def read_text(path, encoding="utf-8"):
        return Path(path).read_text(encoding=encoding)

    def read_bytes(path):
        return Path(path).read_bytes()

    def read_json(path):
        return json.loads(Path(path).read_text())

    def read_lines(path, limit=None):
        lines = Path(path).read_text().splitlines()
        return lines[: int(limit)] if limit else lines

    def write_text(path, content, encoding="utf-8"):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        # Invalidate bytecode so subsequent process runs load the new source
        if p.suffix == ".py":
            try:
                import shutil as _sh
                cache = p.parent / "__pycache__"
                if cache.is_dir():
                    _sh.rmtree(cache, ignore_errors=True)
            except Exception:
                pass
        return {"path": str(p), "bytes": len(content.encode(encoding))}

    def write_json(path, obj, indent=2):
        return write_text(path, json.dumps(obj, indent=int(indent), default=str))

    def append_text(path, content):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(content)
        return {"path": str(p), "appended": len(content)}

    def delete_file(path):
        p = Path(path)
        existed = p.exists()
        if existed:
            p.unlink()
        return {"path": str(p), "deleted": existed}

    def list_dir(path, pattern="*", recursive=False):
        p = Path(path)
        it = p.rglob(pattern) if recursive else p.glob(pattern)
        return [str(x) for x in sorted(it)]

    def make_dir(path):
        Path(path).mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "created": True}

    def copy_file(source, destination):
        data = Path(source).read_bytes()
        dst = Path(destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return {"source": str(source), "destination": str(dst), "bytes": len(data)}

    def move_file(source, destination):
        copy_file(source, destination)
        Path(source).unlink()
        return {"source": str(source), "destination": str(destination), "moved": True}

    def file_info(path):
        p = Path(path)
        if not p.exists():
            return {"path": str(p), "exists": False}
        st = p.stat()
        return {"path": str(p), "exists": True, "size": st.st_size,
                "modified": st.st_mtime, "is_dir": p.is_dir()}

    def hash_file(path, algorithm="sha256"):
        h = hashlib.new(algorithm)
        with Path(path).open("rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
        return h.hexdigest()

    defs = [
        ("read_text", read_text, {"path": STR, "encoding": OPT(STR)}, STR, R, "Read a text file."),
        ("read_bytes", read_bytes, {"path": STR}, BYTES, R, "Read a binary file."),
        ("read_json", read_json, {"path": STR}, ANY, R, "Read and parse JSON."),
        ("read_lines", read_lines, {"path": STR, "limit": OPT(INT)}, LIST(STR), R, "Read a file as lines."),
        ("exists", lambda path: Path(path).exists(), {"path": STR}, BOOL, R, "Path existence test."),
        ("list_dir", list_dir, {"path": STR, "pattern": OPT(STR), "recursive": OPT(BOOL)}, LIST(STR), R, "Glob a directory."),
        ("file_info", file_info, {"path": STR}, DICT(), R, "Size, mtime and kind."),
        ("hash_file", hash_file, {"path": STR, "algorithm": OPT(STR)}, STR, R, "Content digest of a file."),
        ("write_text", write_text, {"path": STR, "content": STR, "encoding": OPT(STR)}, DICT(), W, "Write a text file."),
        ("write_json", write_json, {"path": STR, "obj": ANY, "indent": OPT(INT)}, DICT(), W, "Serialize an object to a file."),
        ("append_text", append_text, {"path": STR, "content": STR}, DICT(), W, "Append to a file."),
        ("delete_file", delete_file, {"path": STR}, DICT(), W, "Remove a file."),
        ("make_dir", make_dir, {"path": STR}, DICT(), W, "Create a directory tree."),
        ("copy_file", copy_file, {"source": STR, "destination": STR}, DICT(), W, "Copy a file."),
        ("move_file", move_file, {"source": STR, "destination": STR}, DICT(), W, "Move a file."),
    ]
    for name, fn, inp, out, eff, doc in defs:
        _p(reg, name, F, fn, inp, out, effects=eff, doc=doc)


# ===========================================================================
# 10. NETWORK  (all gated; provider-backed where a service is required)
# ===========================================================================

def register_network(reg: PrimitiveRegistry) -> None:
    F = "network"
    NET = (Effect.NETWORK,)

    async def http_request(ctx, url, method="GET", headers=None, body=None, timeout=30.0):
        provider = _provider(ctx, "http")
        if provider is None:
            raise RuntimeError(
                "no http provider configured; register one via "
                "engine.providers.register('http', ...) — the substrate does not "
                "open sockets on its own"
            )
        return await provider(url=url, method=method, headers=headers or {},
                              body=body, timeout=float(timeout))

    async def http_get(ctx, url, headers=None):
        return await http_request(ctx, url, "GET", headers)

    async def http_post(ctx, url, body, headers=None):
        return await http_request(ctx, url, "POST", headers, body)

    async def web_search(ctx, query, limit=10):
        provider = _provider(ctx, "web_search")
        if provider is None:
            raise RuntimeError("no web_search provider configured")
        return await provider(query=query, limit=int(limit))

    async def fetch_url(ctx, url):
        r = await http_get(ctx, url)
        return r.get("body", "") if isinstance(r, dict) else str(r)

    async def download(ctx, url, path):
        data = await fetch_url(ctx, url)
        ctx.governor.check(Effect.WRITE_FS, path, "network.download")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else str(data))
        return {"url": url, "path": str(p), "bytes": len(str(data))}

    def parse_url(url):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(url)
        return {"scheme": u.scheme, "host": u.netloc, "path": u.path,
                "query": {k: v[0] for k, v in parse_qs(u.query).items()}}

    def build_url(base, params):
        from urllib.parse import urlencode
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{urlencode(params)}" if params else base

    _p(reg, "http_request", F, http_request,
       {"url": STR, "method": OPT(STR), "headers": OPT(DICT()), "body": OPT(ANY), "timeout": OPT(NUM)},
       DICT(), effects=NET, needs_ctx=True, doc="Issue an HTTP request through the configured provider.")
    _p(reg, "http_get", F, http_get, {"url": STR, "headers": OPT(DICT())}, DICT(),
       effects=NET, needs_ctx=True, doc="HTTP GET.")
    _p(reg, "http_post", F, http_post, {"url": STR, "body": ANY, "headers": OPT(DICT())}, DICT(),
       effects=NET, needs_ctx=True, doc="HTTP POST.")
    _p(reg, "fetch_url", F, fetch_url, {"url": STR}, STR, effects=NET, needs_ctx=True,
       doc="Fetch a URL and return its body.")
    _p(reg, "web_search", F, web_search, {"query": STR, "limit": OPT(INT)}, LIST(DICT()),
       effects=NET, needs_ctx=True, doc="Search the web via the configured provider.")
    _p(reg, "download", F, download, {"url": STR, "path": STR}, DICT(),
       effects=(Effect.NETWORK, Effect.WRITE_FS), needs_ctx=True, doc="Fetch a URL to disk.")
    _p(reg, "parse_url", F, parse_url, {"url": STR}, DICT(), doc="Decompose a URL.")
    _p(reg, "build_url", F, build_url, {"base": STR, "params": DICT()}, STR, doc="Append a query string.")


def _provider(ctx: ExecContext, name: str):
    providers = getattr(ctx, "providers", None)
    if providers is None:
        return None
    if hasattr(providers, "get"):
        return providers.get(name)
    return getattr(providers, name, None)


# ===========================================================================
# 11. KNOWLEDGE  (engine memory — the KnowledgeBase)
# ===========================================================================

def register_knowledge(reg: PrimitiveRegistry) -> None:
    F = "knowledge"
    MEM = (Effect.MEMORY,)

    def _facts_table(ctx):
        kb = ctx.kb
        if kb is None:
            raise RuntimeError("no knowledge base bound to this execution context")
        with kb._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS facts (
                key TEXT PRIMARY KEY, value TEXT, tags TEXT,
                confidence REAL DEFAULT 1.0, source TEXT, updated_at REAL)""")
        return kb

    def remember(ctx, key, value, tags=None, confidence=1.0, source=""):
        kb = _facts_table(ctx)
        with kb._conn() as c:
            c.execute("INSERT OR REPLACE INTO facts (key,value,tags,confidence,source,updated_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (key, json.dumps(value, default=str), json.dumps(tags or []),
                       float(confidence), source, time.time()))
        return {"key": key, "stored": True}

    def recall(ctx, key, default=None):
        kb = _facts_table(ctx)
        with kb._conn() as c:
            row = c.execute("SELECT value FROM facts WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def recall_with_confidence(ctx, key):
        kb = _facts_table(ctx)
        with kb._conn() as c:
            row = c.execute("SELECT value,confidence,source,updated_at FROM facts WHERE key=?",
                            (key,)).fetchone()
        if not row:
            return {"found": False}
        return {"found": True, "value": json.loads(row[0]), "confidence": row[1],
                "source": row[2], "updated_at": row[3]}

    def forget(ctx, key):
        kb = _facts_table(ctx)
        with kb._conn() as c:
            cur = c.execute("DELETE FROM facts WHERE key=?", (key,))
        return {"key": key, "removed": cur.rowcount > 0}

    def search_knowledge(ctx, term, limit=20):
        kb = _facts_table(ctx)
        with kb._conn() as c:
            rows = c.execute("SELECT key,value,confidence FROM facts "
                             "WHERE key LIKE ? OR value LIKE ? OR tags LIKE ? LIMIT ?",
                             (f"%{term}%", f"%{term}%", f"%{term}%", int(limit))).fetchall()
        return [{"key": k, "value": json.loads(v), "confidence": c_} for k, v, c_ in rows]

    def list_facts(ctx, prefix="", limit=100):
        kb = _facts_table(ctx)
        with kb._conn() as c:
            rows = c.execute("SELECT key FROM facts WHERE key LIKE ? ORDER BY updated_at DESC LIMIT ?",
                             (f"{prefix}%", int(limit))).fetchall()
        return [r[0] for r in rows]

    def associate(ctx, key_a, key_b, relation="related"):
        return remember(ctx, f"link:{key_a}:{relation}:{key_b}",
                        {"from": key_a, "to": key_b, "relation": relation}, tags=["link", relation])

    def linked(ctx, key, relation=None):
        pat = f"link:{key}:" + (f"{relation}:" if relation else "")
        return [f["value"] for f in search_knowledge(ctx, pat, 100)
                if str(f["key"]).startswith(pat)]

    def rank_by_confidence(ctx, keys):
        out = [recall_with_confidence(ctx, k) | {"key": k} for k in keys]
        return sorted([o for o in out if o.get("found")], key=lambda o: -o["confidence"])

    def get_capability(ctx, capability_id):
        if ctx.kb is None:
            raise RuntimeError("no knowledge base bound")
        return ctx.kb.get_capability(capability_id) or {}

    def capability_success_rate(ctx, capability_id):
        rec = get_capability(ctx, capability_id)
        used = rec.get("use_count", 0)
        return (rec.get("success_count", 0) / used) if used else None

    defs = [
        ("remember", remember, {"key": STR, "value": ANY, "tags": OPT(LIST(STR)),
                                "confidence": OPT(NUM), "source": OPT(STR)}, DICT(), "Persist a fact."),
        ("recall", recall, {"key": STR, "default": OPT(ANY)}, ANY, "Retrieve a fact."),
        ("recall_with_confidence", recall_with_confidence, {"key": STR}, DICT(), "Retrieve a fact with provenance."),
        ("forget", forget, {"key": STR}, DICT(), "Delete a fact."),
        ("search_knowledge", search_knowledge, {"term": STR, "limit": OPT(INT)}, LIST(DICT()), "Substring search over stored facts."),
        ("list_facts", list_facts, {"prefix": OPT(STR), "limit": OPT(INT)}, LIST(STR), "Enumerate fact keys."),
        ("associate", associate, {"key_a": STR, "key_b": STR, "relation": OPT(STR)}, DICT(), "Link two facts."),
        ("linked", linked, {"key": STR, "relation": OPT(STR)}, LIST(), "Facts linked to a key."),
        ("rank_by_confidence", rank_by_confidence, {"keys": LIST(STR)}, LIST(DICT()), "Order facts by confidence."),
        ("get_capability", get_capability, {"capability_id": STR}, DICT(), "Load a stored capability record."),
        ("capability_success_rate", capability_success_rate, {"capability_id": STR}, OPT(FLOAT), "Historical pass rate."),
    ]
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn, inp, out, effects=MEM, needs_ctx=True, doc=doc)


# ===========================================================================
# 12. OBSERVATION
# ===========================================================================

def register_observation(reg: PrimitiveRegistry) -> None:
    F = "observation"

    def inspect_value(value):
        t = infer(value)
        info = {"type": str(t), "python_type": type(value).__name__}
        if hasattr(value, "__len__"):
            info["length"] = len(value)
        if isinstance(value, dict):
            info["fields"] = sorted(value)[:50]
        if isinstance(value, list) and value:
            info["element_types"] = sorted({str(infer(v)) for v in value[:100]})
        return info

    def profile_records(items):
        """Column-level profile of a list of records — the workhorse of any
        data-understanding capability."""
        if not items:
            return {"records": 0, "columns": {}}
        cols: Dict[str, Dict[str, Any]] = {}
        for rec in items:
            for k, v in (rec or {}).items():
                c = cols.setdefault(k, {"present": 0, "nulls": 0, "types": set(), "samples": []})
                c["present"] += 1
                if v is None:
                    c["nulls"] += 1
                else:
                    c["types"].add(str(infer(v)))
                    if len(c["samples"]) < 3:
                        c["samples"].append(v)
        n = len(items)
        return {"records": n, "columns": {
            k: {"present": c["present"], "coverage": round(c["present"] / n, 3),
                "nulls": c["nulls"], "types": sorted(c["types"]), "samples": c["samples"]}
            for k, c in cols.items()}}

    def detect_anomalies(values, threshold=3.0):
        import statistics
        if len(values) < 3:
            return []
        mu, sd = statistics.fmean(values), statistics.pstdev(values)
        if sd == 0:
            return []
        return [{"index": i, "value": v, "zscore": round((v - mu) / sd, 3)}
                for i, v in enumerate(values) if abs((v - mu) / sd) > threshold]

    def classify_shape(value):
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return "records"
        if isinstance(value, list) and value and all(isinstance(v, (int, float)) for v in value):
            return "series"
        if isinstance(value, dict):
            return "record"
        if isinstance(value, str):
            return "text"
        if isinstance(value, (int, float)):
            return "scalar"
        return "unknown"

    def measure(value, metric):
        fns = {
            "length": lambda v: len(v),
            "depth": lambda v: _depth(v),
            "size_bytes": lambda v: len(json.dumps(v, default=str).encode()),
            "distinct": lambda v: len({json.dumps(x, sort_keys=True, default=str) for x in v}),
        }
        if metric not in fns:
            raise ValueError(f"unknown metric {metric!r}; have {sorted(fns)}")
        return fns[metric](value)

    def _depth(v, d=0):
        if isinstance(v, dict):
            return max([_depth(x, d + 1) for x in v.values()] or [d + 1])
        if isinstance(v, list):
            return max([_depth(x, d + 1) for x in v] or [d + 1])
        return d

    async def analyze_image(ctx, path, task="describe"):
        provider = _provider(ctx, "vision")
        if provider is None:
            raise RuntimeError("no vision provider configured")
        ctx.governor.check(Effect.READ_FS, path, "observation.analyze_image")
        return await provider(path=path, task=task)

    async def transcribe_audio(ctx, path):
        provider = _provider(ctx, "audio")
        if provider is None:
            raise RuntimeError("no audio provider configured")
        ctx.governor.check(Effect.READ_FS, path, "observation.transcribe_audio")
        return await provider(path=path)

    for name, fn, inp, out, doc in [
        ("inspect", inspect_value, {"value": ANY}, DICT(), "Structural description of any value."),
        ("profile_records", profile_records, {"items": LIST(DICT())}, DICT(), "Per-column coverage, types and samples."),
        ("detect_anomalies", detect_anomalies, {"values": LIST(NUM), "threshold": OPT(NUM)}, LIST(DICT()), "Z-score outliers."),
        ("classify_shape", classify_shape, {"value": ANY}, STR, "Name the shape of a value."),
        ("measure", measure, {"value": ANY, "metric": STR}, NUM, "Length, depth, byte size or distinct count."),
    ]:
        _p(reg, name, F, fn, inp, out, doc=doc)

    _p(reg, "analyze_image", F, analyze_image, {"path": STR, "task": OPT(STR)}, DICT(),
       effects=(Effect.READ_FS, Effect.NETWORK), needs_ctx=True, doc="Vision analysis via provider.")
    _p(reg, "transcribe_audio", F, transcribe_audio, {"path": STR}, DICT(),
       effects=(Effect.READ_FS, Effect.NETWORK), needs_ctx=True, doc="Speech-to-text via provider.")


# ===========================================================================
# 13. CONFIG / ENVIRONMENT
# ===========================================================================

def register_config(reg: PrimitiveRegistry) -> None:
    F = "config"

    def get_config(ctx, key, default=None):
        cfg = ctx.scratch.setdefault("_config", {})
        return cfg.get(key, default)

    def set_config(ctx, key, value):
        ctx.scratch.setdefault("_config", {})[key] = value
        return {"key": key, "set": True}

    def get_env(ctx, name, default=None):
        ctx.governor.check(Effect.CREDENTIAL, name, "config.get_env")
        return os.environ.get(name, default)

    def feature_enabled(ctx, flag):
        return bool(ctx.scratch.setdefault("_config", {}).get(f"feature.{flag}", False))

    def platform_info():
        import platform as _pl
        return {"system": _pl.system(), "release": _pl.release(),
                "python": _pl.python_version(), "machine": _pl.machine()}

    def resource_reserve(ctx, name, amount):
        pool = ctx.scratch.setdefault("_resources", {})
        held = pool.get(name, 0)
        pool[name] = held + float(amount)
        return {"resource": name, "held": pool[name]}

    def resource_release(ctx, name, amount):
        pool = ctx.scratch.setdefault("_resources", {})
        pool[name] = max(0.0, pool.get(name, 0) - float(amount))
        return {"resource": name, "held": pool[name]}

    _p(reg, "get_config", F, get_config, {"key": STR, "default": OPT(ANY)}, ANY,
       effects=(Effect.MEMORY,), needs_ctx=True, doc="Read a run-scoped setting.")
    _p(reg, "set_config", F, set_config, {"key": STR, "value": ANY}, DICT(),
       effects=(Effect.MEMORY,), needs_ctx=True, doc="Write a run-scoped setting.")
    _p(reg, "get_env", F, get_env, {"name": STR, "default": OPT(ANY)}, ANY,
       effects=(Effect.CREDENTIAL,), needs_ctx=True, doc="Read an environment variable (gated).")
    _p(reg, "feature_enabled", F, feature_enabled, {"flag": STR}, BOOL,
       effects=(Effect.MEMORY,), needs_ctx=True, doc="Feature flag test.")
    _p(reg, "platform_info", F, platform_info, {}, DICT(), doc="Host OS and interpreter details.")
    _p(reg, "resource_reserve", F, resource_reserve, {"name": STR, "amount": NUM}, DICT(),
       effects=(Effect.MEMORY,), needs_ctx=True, doc="Claim a share of a named resource.")
    _p(reg, "resource_release", F, resource_release, {"name": STR, "amount": NUM}, DICT(),
       effects=(Effect.MEMORY,), needs_ctx=True, doc="Return a share of a named resource.")


# ===========================================================================
# 14. CACHING
# ===========================================================================

def register_caching(reg: PrimitiveRegistry) -> None:
    F = "caching"

    def _cache(ctx):
        return ctx.scratch.setdefault("_cache", {})

    def cache_set(ctx, key, value, ttl=None):
        _cache(ctx)[key] = {"value": value, "at": time.time(),
                            "ttl": float(ttl) if ttl else None}
        return {"key": key, "cached": True}

    def cache_get(ctx, key, default=None):
        entry = _cache(ctx).get(key)
        if not entry:
            return default
        if entry["ttl"] and time.time() - entry["at"] > entry["ttl"]:
            _cache(ctx).pop(key, None)
            return default
        return entry["value"]

    def cache_has(ctx, key):
        return cache_get(ctx, key, _MISS) is not _MISS

    def cache_invalidate(ctx, key):
        return {"key": key, "removed": _cache(ctx).pop(key, None) is not None}

    def cache_clear(ctx, prefix=""):
        c = _cache(ctx)
        doomed = [k for k in c if str(k).startswith(prefix)]
        for k in doomed:
            c.pop(k, None)
        return {"cleared": len(doomed)}

    def cache_stats(ctx):
        c = _cache(ctx)
        return {"entries": len(c), "keys": sorted(map(str, c))[:100]}

    async def memoize(ctx, key, producer, ttl=None):
        """Return a cached value or compute-and-store it. `producer` is a
        zero-arg callable, so this works for any computation the composer can
        express as a closure."""
        hit = cache_get(ctx, key, _MISS)
        if hit is not _MISS:
            return {"value": hit, "hit": True}
        value = producer()
        if asyncio.iscoroutine(value):
            value = await value
        cache_set(ctx, key, value, ttl)
        return {"value": value, "hit": False}

    for name, fn, inp, out, doc in [
        ("cache_set", cache_set, {"key": STR, "value": ANY, "ttl": OPT(NUM)}, DICT(), "Store a value with optional TTL."),
        ("cache_get", cache_get, {"key": STR, "default": OPT(ANY)}, ANY, "Read a cached value."),
        ("cache_has", cache_has, {"key": STR}, BOOL, "Is the key live?"),
        ("cache_invalidate", cache_invalidate, {"key": STR}, DICT(), "Drop one entry."),
        ("cache_clear", cache_clear, {"prefix": OPT(STR)}, DICT(), "Drop entries by prefix."),
        ("cache_stats", cache_stats, {}, DICT(), "Cache size and keys."),
        ("memoize", memoize, {"key": STR, "producer": CALLABLE, "ttl": OPT(NUM)}, DICT(), "Compute once, reuse thereafter."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=(Effect.MEMORY,), needs_ctx=True, doc=doc)


_MISS = object()


# ===========================================================================
# 15. CONCURRENCY
# ===========================================================================

def register_concurrency(reg: PrimitiveRegistry) -> None:
    F = "concurrency"
    SP = (Effect.SPAWN,)

    async def _call(fn, arg=None):
        r = fn() if arg is None else fn(arg)
        if asyncio.iscoroutine(r):
            r = await r
        return r

    async def parallel_map(items, fn, limit=8):
        sem = asyncio.Semaphore(int(limit))

        async def one(x):
            async with sem:
                return await _call(fn, x)
        return list(await asyncio.gather(*(one(x) for x in items)))

    async def gather_all(tasks):
        return list(await asyncio.gather(*(_call(t) for t in tasks)))

    async def race(tasks, timeout=None):
        coros = [asyncio.ensure_future(_call(t)) for t in tasks]
        done, pending = await asyncio.wait(coros, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        if not done:
            raise TimeoutError("no task completed in time")
        return next(iter(done)).result()

    async def with_timeout(fn, seconds):
        try:
            return {"ok": True, "value": await asyncio.wait_for(_call(fn), float(seconds))}
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"timed out after {seconds}s"}

    async def batch_process(items, fn, batch_size=10):
        out = []
        for i in range(0, len(items), int(batch_size)):
            chunk = items[i: i + int(batch_size)]
            out.extend(await asyncio.gather(*(_call(fn, x) for x in chunk)))
        return out

    async def sequential(tasks):
        return [await _call(t) for t in tasks]

    for name, fn, inp, out, doc in [
        ("parallel_map", parallel_map, {"items": LIST(), "fn": CALLABLE, "limit": OPT(INT)}, LIST(), "Apply fn concurrently with a concurrency cap."),
        ("gather_all", gather_all, {"tasks": LIST(CALLABLE)}, LIST(), "Run every thunk concurrently, keep order."),
        ("race", race, {"tasks": LIST(CALLABLE), "timeout": OPT(NUM)}, ANY, "First result wins; the rest are cancelled."),
        ("with_timeout", with_timeout, {"fn": CALLABLE, "seconds": NUM}, DICT(), "Bound a computation in time."),
        ("batch_process", batch_process, {"items": LIST(), "fn": CALLABLE, "batch_size": OPT(INT)}, LIST(), "Process in fixed-size concurrent batches."),
        ("sequential", sequential, {"tasks": LIST(CALLABLE)}, LIST(), "Run thunks strictly in order."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=SP, doc=doc)


# ===========================================================================
# 16. DEBUGGING / INTROSPECTION
# ===========================================================================

def register_debugging(reg: PrimitiveRegistry) -> None:
    F = "debugging"

    def log(ctx, message, level="info", data=None):
        entry = {"at": time.time(), "level": level, "message": str(message), "data": data}
        ctx.scratch.setdefault("_log", []).append(entry)
        return entry

    def get_log(ctx, level=None):
        entries = ctx.scratch.get("_log", [])
        return [e for e in entries if level is None or e["level"] == level]

    def trace(ctx):
        return list(ctx.trace)

    def trace_summary(ctx):
        totals: Dict[str, Dict[str, float]] = {}
        for e in ctx.trace:
            t = totals.setdefault(e["primitive"], {"calls": 0, "ms": 0.0})
            t["calls"] += 1
            t["ms"] += e["ms"]
        ranked = sorted(totals.items(), key=lambda kv: -kv[1]["ms"])
        return {"total_calls": len(ctx.trace),
                "total_ms": round(sum(e["ms"] for e in ctx.trace), 3),
                "by_primitive": [{"primitive": k, **v} for k, v in ranked[:20]]}

    def explain_error(error):
        return {"kind": type(error).__name__ if isinstance(error, BaseException) else "str",
                "message": str(error),
                "likely_cause": _cause(str(error))}

    def _cause(msg):
        m = msg.lower()
        if "missing required argument" in m:
            return "a plan step did not bind every declared input"
        if "expects" in m and "got" in m:
            return "type mismatch between a producer's output and a consumer's input"
        if "denied" in m:
            return "the capability attempted an effect it was not granted"
        if "division by zero" in m:
            return "an unguarded divisor reached zero; add a logic.not_equals guard"
        if "unknown primitive" in m:
            return "the plan referenced a name that is not in the registry"
        return "unclassified"

    async def profile(fn, iterations=1):
        started = time.time()
        result = None
        for _ in range(int(iterations)):
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
        elapsed = (time.time() - started) * 1000
        return {"iterations": int(iterations), "total_ms": round(elapsed, 3),
                "per_call_ms": round(elapsed / max(1, int(iterations)), 4), "last_result": result}

    def governor_report(ctx):
        return ctx.governor.summary() | {
            "recent_denials": [{"effect": d.effect.value, "target": d.target,
                                "primitive": d.primitive} for d in ctx.governor.denials()[-10:]]}

    for name, fn, inp, out, ctxflag, doc in [
        ("log", log, {"message": ANY, "level": OPT(STR), "data": OPT(ANY)}, DICT(), True, "Append to the run log."),
        ("get_log", get_log, {"level": OPT(STR)}, LIST(DICT()), True, "Read the run log."),
        ("trace", trace, {}, LIST(DICT()), True, "Per-call execution trace."),
        ("trace_summary", trace_summary, {}, DICT(), True, "Aggregate the trace by primitive."),
        ("governor_report", governor_report, {}, DICT(), True, "Grants, audits and recent denials."),
        ("explain_error", explain_error, {"error": ANY}, DICT(), False, "Classify a failure into a likely cause."),
        ("profile", profile, {"fn": CALLABLE, "iterations": OPT(INT)}, DICT(), False, "Time a computation."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=(Effect.MEMORY,), needs_ctx=ctxflag, doc=doc)



def register_process(reg: PrimitiveRegistry) -> None:
    """Governed process execution — PROCESS effect family (M+29.00)."""
    import subprocess as _sp
    F = "process"
    P = (Effect.PROCESS,)

    def run_command(ctx, command, cwd=None, timeout=60):
        ctx.governor.check(Effect.PROCESS, str(command), "run_command")
        completed = _sp.run(
            command if isinstance(command, list) else ["bash", "-lc", str(command)],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ok": completed.returncode == 0,
        }

    def run_python(ctx, path, args=None, cwd=None, timeout=60):
        ctx.governor.check(Effect.PROCESS, str(path), "run_python")
        # Drop stale bytecode so repaired .py sources are actually loaded
        try:
            from pathlib import Path as _P
            import shutil as _sh
            src_dir = _P(path).resolve().parent
            cache = src_dir / "__pycache__"
            if cache.is_dir():
                _sh.rmtree(cache, ignore_errors=True)
            for sib in src_dir.glob("*.pyc"):
                try:
                    sib.unlink()
                except OSError:
                    pass
        except Exception:
            pass
        cmd = ["python", "-B", str(path)] + list(args or [])
        completed = _sp.run(
            cmd, cwd=cwd or None, capture_output=True, text=True, timeout=float(timeout),
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ok": completed.returncode == 0,
        }

    _p(reg, "run_command", F, run_command,
       {"command": STR, "cwd": OPT(STR), "timeout": OPT(NUM)}, DICT(),
       effects=P, needs_ctx=True, doc="Run a shell command under PROCESS governance.")
    _p(reg, "run_python", F, run_python,
       {"path": STR, "args": OPT(LIST()), "cwd": OPT(STR), "timeout": OPT(NUM)}, DICT(),
       effects=P, needs_ctx=True, doc="Run a Python file under PROCESS governance.")


def register_all_effect(reg: PrimitiveRegistry) -> None:
    register_process(reg)
    register_filesystem(reg)
    register_network(reg)
    register_knowledge(reg)
    register_observation(reg)
    register_config(reg)
    register_caching(reg)
    register_concurrency(reg)
    register_debugging(reg)
