"""Causal tests for zip archive inspection on ScopedFileService.

Every attack below is EXECUTED against a real crafted archive on a real
filesystem: zip-slip, absolute paths, Windows drive/backslash paths, symlink
entries, encoded traversal, decompression bombs (binary-patched central
directory), oversized reads, out-of-scope archive/dest, read-only scope,
tar.gz masquerade. Every refusal is observed, not asserted from code.
"""
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.files import (  # noqa: E402
    ScopedFileService,
    PROVENANCE_NAME,
    _check_entry_name,
)


# -- archive builders ----------------------------------------------------
def _write_zip(path, entries):
    """entries: list of (name, bytes) or (ZipInfo, bytes)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in entries:
            if isinstance(item[0], zipfile.ZipInfo):
                zf.writestr(item[0], item[1])
            else:
                zf.writestr(item[0], item[1])


def _symlink_info(name, target):
    zi = zipfile.ZipInfo(name)
    zi.create_system = 3
    zi.external_attr = (0o120755 << 16)
    return zi, target.encode()


def _patch_central_uncompressed_size(path, claimed):
    """Rewrite the central directory's uncompressed-size field (offset 24)
    for every entry — the archive then LIES about its sizes."""
    with open(path, "r+b") as fh:
        data = fh.read()
        out = bytearray(data)
        idx = 0
        patched = 0
        while True:
            idx = out.find(b"PK\x01\x02", idx)
            if idx == -1:
                break
            struct.pack_into("<I", out, idx + 24, claimed)
            patched += 1
            idx += 4
        assert patched > 0, "no central directory entries found"
        fh.seek(0)
        fh.write(out)
        fh.truncate()


def _svc(writable=True):
    td = tempfile.TemporaryDirectory()
    return td, ScopedFileService(td.name, writable=writable)


def _archive_in(svc_td, name, entries):
    path = os.path.join(svc_td.name, name)
    _write_zip(path, entries)
    return name


MIXED = [
    ("hello.txt", b"hello world"),
    ("sub/deep/note.txt", "déeper nötè — ünïcödé ✓".encode("utf-8")),
    ("sub/empty.txt", b""),
    ("data.bin", bytes(range(256)) * 40),  # 10240 bytes, all byte values
    ("sub/deep/deeper/final.md", b"# final\n\n* a\n* b\n"),
    ("ünïcödé-dir/ünïcödé-file.txt", b"unicode paths work"),
]


class TestListArchive(unittest.TestCase):
    def setUp(self):
        self.td, self.svc = _svc()

    def tearDown(self):
        self.td.cleanup()

    def test_list_accurate_names_sizes(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        r = self.svc.list_archive(rel)
        self.assertTrue(r["ok"], r)
        got = {e["name"]: e for e in r["entries"]}
        for name, data in MIXED:
            self.assertIn(name, got)
            self.assertEqual(got[name]["size_bytes"], len(data))
            self.assertFalse(got[name]["is_dir"])
            self.assertTrue(got[name]["safe"])
        self.assertEqual(r["entry_count"], len(MIXED))

    def test_list_non_zip_refused(self):
        import tarfile
        tpath = os.path.join(self.td.name, "a.tar.gz")
        with tarfile.open(tpath, "w:gz") as tf:
            info = tarfile.TarInfo("x.txt")
            payload = b"tarred"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        r = self.svc.list_archive("a.tar.gz")
        self.assertFalse(r["ok"])
        self.assertIn("not a zip", r["error"])

    def test_list_missing_archive(self):
        r = self.svc.list_archive("nope.zip")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_list_flags_unsafe_entries(self):
        rel = _archive_in(self.td, "evil.zip", [
            ("good.txt", b"ok"),
            ("../../evil.txt", b"x"),
            ("/abs/evil.txt", b"x"),
            _symlink_info("link", "/etc/passwd"),
        ])
        r = self.svc.list_archive(rel)
        self.assertTrue(r["ok"], r)
        got = {e["name"]: e for e in r["entries"]}
        self.assertTrue(got["good.txt"]["safe"])
        self.assertFalse(got["../../evil.txt"]["safe"])
        self.assertIn("dot-dot", got["../../evil.txt"]["unsafe_reason"])
        self.assertFalse(got["/abs/evil.txt"]["safe"])
        self.assertTrue(got["link"]["is_symlink"])
        self.assertFalse(got["link"]["safe"])

    def test_list_too_many_entries_refused(self):
        rel = _archive_in(self.td, "many.zip",
                          [(f"f{i}.txt", b"x") for i in range(6)])
        r = self.svc.list_archive(rel, max_entries=5)
        self.assertFalse(r["ok"])
        self.assertIn("too many entries", r["error"])

    def test_entry_name_check_unit(self):
        self.assertFalse(_check_entry_name("../../evil")[0])
        self.assertFalse(_check_entry_name("/abs/x")[0])
        self.assertFalse(_check_entry_name("C:\\win")[0])
        self.assertFalse(_check_entry_name("a\\b")[0])
        self.assertFalse(_check_entry_name("a//b")[0])
        self.assertTrue(_check_entry_name("a/b/c.txt")[0])
        self.assertTrue(_check_entry_name("ünïcödé/f ✓.txt")[0])


class TestReadInside(unittest.TestCase):
    def setUp(self):
        self.td, self.svc = _svc()

    def tearDown(self):
        self.td.cleanup()

    def test_read_inside_byte_exact(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        for name, data in MIXED:
            r = self.svc.read_inside(rel, name)
            self.assertTrue(r["ok"], (name, r))
            self.assertEqual(r["data"], data)
            self.assertEqual(r["size_bytes"], len(data))

    def test_read_inside_not_found(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        r = self.svc.read_inside(rel, "missing.txt")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_read_inside_unsafe_name_refused(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        r = self.svc.read_inside(rel, "../../evil.txt")
        self.assertFalse(r["ok"])
        self.assertIn("unsafe entry name", r["error"])
        # the refusal is logged
        self.assertTrue(any(e["entry"] == "../../evil.txt"
                            for e in self.svc.refusals()))

    def test_read_inside_symlink_refused(self):
        rel = _archive_in(self.td, "s.zip", [
            ("real.txt", b"data"),
            _symlink_info("link", "/etc/passwd"),
        ])
        r = self.svc.read_inside(rel, "link")
        self.assertFalse(r["ok"])
        self.assertIn("symlink", r["error"])

    def test_read_inside_dir_refused(self):
        rel = _archive_in(self.td, "d.zip", [("sub/", b""),
                                             ("sub/f.txt", b"x")])
        r = self.svc.read_inside(rel, "sub/")
        self.assertFalse(r["ok"])
        self.assertIn("not a file", r["error"])

    def test_read_inside_oversize_refused(self):
        rel = _archive_in(self.td, "m.zip", [("big.bin", b"z" * 5000)])
        r = self.svc.read_inside(rel, "big.bin", max_bytes=100)
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])

    def test_read_inside_bomb_claim_refused_without_allocating(self):
        path = os.path.join(self.td.name, "bomb.zip")
        _write_zip(path, [("payload.bin", b"tiny")])
        _patch_central_uncompressed_size(path, 4_000_000_000)  # claims ~3.7 GiB
        r = self.svc.read_inside("bomb.zip", "payload.bin")
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.td, self.svc = _svc(writable=True)

    def tearDown(self):
        self.td.cleanup()

    def test_extract_happy_path_byte_exact(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["skipped"], [])
        self.assertEqual(sorted(r["extracted"]), sorted(n for n, _ in MIXED))
        for name, data in MIXED:
            with open(os.path.join(self.td.name, "out", name), "rb") as fh:
                self.assertEqual(fh.read(), data, name)

    def test_extract_provenance_manifest(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        mpath = os.path.join(self.td.name, "out", PROVENANCE_NAME)
        with open(mpath, encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual(man["archive"], rel)
        self.assertIn("extracted_at", man)
        got = {e["name"]: e for e in man["entries"]}
        for name, data in MIXED:
            self.assertEqual(got[name]["size_bytes"], len(data))
            self.assertEqual(got[name]["sha256"],
                             hashlib.sha256(data).hexdigest())

    def test_extract_zipslip_neutralized(self):
        rel = _archive_in(self.td, "evil.zip", [
            ("good.txt", b"fine"),
            ("../../evil.txt", b"pwned"),
            ("sub/../../../evil2.txt", b"pwned"),
        ])
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["extracted"], ["good.txt"])
        self.assertEqual(len(r["skipped"]), 2)
        # nothing escaped the sandbox
        self.assertFalse(os.path.exists(os.path.join(self.td.name, "evil.txt")))
        parent = os.path.dirname(self.td.name)
        self.assertFalse(os.path.exists(os.path.join(parent, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(parent, "evil2.txt")))
        # refusals logged with reasons
        reasons = [e["reason"] for e in self.svc.refusals()]
        self.assertTrue(any("dot-dot" in x for x in reasons), reasons)

    def test_extract_absolute_and_windows_paths_skipped(self):
        rel = _archive_in(self.td, "evil2.zip", [
            ("/abs/evil.txt", b"x"),
            ("C:\\win.txt", b"x"),
            ("..\\..\\win2.txt", b"x"),
            ("ok.txt", b"fine"),
        ])
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["extracted"], ["ok.txt"])
        self.assertEqual(len(r["skipped"]), 3)

    def test_extract_symlink_never_materialized(self):
        rel = _archive_in(self.td, "s.zip", [
            ("real.txt", b"data"),
            _symlink_info("link", "/etc/passwd"),
            _symlink_info("sub/link2", "real.txt"),
        ])
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["extracted"], ["real.txt"])
        self.assertEqual(len(r["skipped"]), 2)
        self.assertFalse(os.path.islink(os.path.join(self.td.name, "out", "link")))
        self.assertFalse(os.path.exists(os.path.join(self.td.name, "out", "link")))

    def test_extract_bomb_claim_refused_whole_archive(self):
        path = os.path.join(self.td.name, "bomb.zip")
        _write_zip(path, [("payload.bin", b"tiny")])
        _patch_central_uncompressed_size(path, 4_000_000_000)
        r = self.svc.extract("bomb.zip", "out")
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])
        outdir = os.path.join(self.td.name, "out")
        self.assertFalse(os.path.exists(outdir) and os.listdir(outdir))

    def test_extract_lying_small_claim_caught_while_streaming(self):
        # central directory claims 10 bytes; the real stream is 1 MB.
        path = os.path.join(self.td.name, "lie.zip")
        big = os.urandom(1024 * 1024)
        _write_zip(path, [("big.bin", big), ("small.txt", b"keep me")])
        _patch_central_uncompressed_size(path, 10)
        r = self.svc.extract("lie.zip", "out", max_entry_bytes=4096)
        self.assertTrue(r["ok"], r)
        # the lying entry was skipped mid-stream, partial file removed
        self.assertFalse(os.path.exists(
            os.path.join(self.td.name, "out", "big.bin")))
        # the honest entry still extracted byte-exact
        self.assertIn("small.txt", r["extracted"])
        with open(os.path.join(self.td.name, "out", "small.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"keep me")

    def test_extract_dest_outside_scope_refused(self):
        rel = _archive_in(self.td, "m.zip", MIXED)
        r = self.svc.extract(rel, "../escape")
        self.assertFalse(r["ok"])
        self.assertIn("root", r["error"])

    def test_extract_archive_outside_scope_refused(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        _write_zip(os.path.join(outside.name, "o.zip"), MIXED)
        r = self.svc.extract("../" + os.path.basename(outside.name) + "/o.zip",
                             "out")
        self.assertFalse(r["ok"])

    def test_extract_readonly_scope_refused(self):
        td2 = tempfile.TemporaryDirectory()
        self.addCleanup(td2.cleanup)
        _write_zip(os.path.join(td2.name, "m.zip"), MIXED)
        ro = ScopedFileService(td2.name, writable=False)
        r = ro.extract("m.zip", "out")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "read-only scope")

    def test_extract_encoded_traversal_is_literal_and_contained(self):
        weird = "..%2f..%2fevil.txt"
        rel = _archive_in(self.td, "enc.zip", [(weird, b"literal")])
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["extracted"], [weird])
        with open(os.path.join(self.td.name, "out", weird), "rb") as fh:
            self.assertEqual(fh.read(), b"literal")

    def test_extract_backslash_refused(self):
        rel = _archive_in(self.td, "bs.zip", [("a\\b.txt", b"x"),
                                              ("ok.txt", b"y")])
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["extracted"], ["ok.txt"])
        self.assertEqual(len(r["skipped"]), 1)

    def test_extract_concurrent_no_interleave(self):
        a = _archive_in(self.td, "a.zip",
                        [(f"a{i}.txt", f"A{i}".encode()) for i in range(8)])
        b = _archive_in(self.td, "b.zip",
                        [(f"b{i}.txt", f"B{i}".encode()) for i in range(8)])
        errors = []

        def worker(rel, dest):
            try:
                r = self.svc.extract(rel, dest)
                if not r["ok"]:
                    errors.append((dest, r))
            except Exception as exc:  # noqa: BLE001
                errors.append((dest, repr(exc)))

        ts = [threading.Thread(target=worker, args=(a, "da")),
              threading.Thread(target=worker, args=(b, "db"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        for i in range(8):
            for dest, prefix in (("da", "A"), ("db", "B")):
                letter = "a" if dest == "da" else "b"
                with open(os.path.join(self.td.name, dest,
                                       f"{letter}{i}.txt"), "rb") as fh:
                    self.assertEqual(fh.read(), f"{prefix}{i}".encode())

    def test_extract_reserved_provenance_name_skipped(self):
        rel = _archive_in(self.td, "p.zip", [
            (PROVENANCE_NAME, b"attacker manifest"),
            ("ok.txt", b"y"),
        ])
        r = self.svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["extracted"], ["ok.txt"])
        self.assertEqual(len(r["skipped"]), 1)
        self.assertIn("reserved", r["skipped"][0]["reason"])
        with open(os.path.join(self.td.name, "out", PROVENANCE_NAME),
                  encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual(man["extractor"],
                         "swarm_engine.services.files.ScopedFileService.extract")


class TestFreshProcess(unittest.TestCase):
    def test_fresh_subprocess_reread(self):
        td, svc = _svc(writable=True)
        self.addCleanup(td.cleanup)
        rel = _archive_in(td, "m.zip", MIXED)
        r = svc.extract(rel, "out")
        self.assertTrue(r["ok"], r)
        pylib = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "pylib"))
        script = (
            "import json, os, sys; "
            "sys.path.insert(0, %r); "
            "from swarm_engine.services.files import ScopedFileService, PROVENANCE_NAME; "
            "svc = ScopedFileService(%r, writable=True); "
            "lr = svc.list_archive(%r); "
            "assert lr['ok'], lr; "
            "rr = svc.read_inside(%r, 'data.bin'); "
            "assert rr['ok'], rr; "
            "blob = open(os.path.join(%r, 'out', 'data.bin'), 'rb').read(); "
            "assert rr['data'] == blob, 'read_inside != extracted bytes'; "
            "man = json.load(open(os.path.join(%r, 'out', PROVENANCE_NAME))); "
            "names = sorted(e['name'] for e in man['entries']); "
            "print('FRESH_OK entries=' + str(len(names))); "
            "print('sha_match=' + str(man['entries'][0]['sha256'] == __import__('hashlib').sha256(open(os.path.join(%r, 'out', man['entries'][0]['name']), 'rb').read()).hexdigest()))"
        ) % (pylib, td.name, rel, rel, td.name, td.name, td.name)
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FRESH_OK", proc.stdout)
        self.assertIn("sha_match=True", proc.stdout)


if __name__ == "__main__":
    unittest.main()
