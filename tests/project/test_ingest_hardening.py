"""Causal battery for zip ingestion hardening (I-18, sibling parity).

Mirrors the threat model of the ScopedFileService archive path (QUEUED-2):
no zip-slip, no absolute/drive paths, no symlink escape, no encoded
traversal, per-entry streaming caps, forged-size refusal. Every attack is
EXECUTED against a real crafted zip through the real ingest_zip entry
point; every refusal is observed, not asserted from code.

Adaptations vs the reference battery (documented, not silently dropped):
- list_archive/read_inside: N/A — the ingestor has no listing/read API;
  the analogous surface (entry-name safety) is covered via ingest refusals
  and _check_zip_entry_name unit cases.
- skip-and-neutralize: N/A by design — ingest_zip refuses the WHOLE
  archive (fail-closed); a partially-ingested hostile project is worse
  than no project.
- provenance manifest / concurrency: file-service concerns, N/A here.
"""
import io
import os
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.project.ingestion import (  # noqa: E402
    ProjectIngestor,
    ZipEntryUnsafeError,
    _check_zip_entry_name,
    _zipinfo_is_symlink,
)


# -- archive builders ----------------------------------------------------
def _write_zip(path, entries, compression=zipfile.ZIP_DEFLATED):
    """entries: list of (name, bytes) or (ZipInfo, bytes)."""
    with zipfile.ZipFile(path, "w", compression) as zf:
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


def _patch_entry_flag_bits(path, target_name, or_bits):
    """OR flag bits into the central-directory header of ONE entry.

    (zipfile.writestr() recomputes flag_bits, so an encrypted entry cannot
    be built through the public API — binary patch, same technique as the
    size patch above.)
    """
    target = target_name.encode("utf-8")
    with open(path, "r+b") as fh:
        data = fh.read()
        out = bytearray(data)
        idx = 0
        patched = 0
        while True:
            idx = out.find(b"PK\x01\x02", idx)
            if idx == -1:
                break
            fn_len = struct.unpack_from("<H", out, idx + 28)[0]
            name = bytes(out[idx + 46:idx + 46 + fn_len])
            if name == target:
                cur = struct.unpack_from("<H", out, idx + 8)[0]
                struct.pack_into("<H", out, idx + 8, cur | or_bits)
                patched += 1
            idx += 4
        assert patched == 1, f"expected 1 header for {target_name!r}"
        fh.seek(0)
        fh.write(out)
        fh.truncate()


MIXED = [
    ("hello.txt", b"hello world"),
    ("sub/deep/note.txt", "deeper note".encode("utf-8")),
    ("sub/empty.txt", b""),
    ("data.bin", bytes(range(256)) * 40),
]




class IngestBattery(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.projects_dir = os.path.join(self.td.name, "projects")
        self.ing = ProjectIngestor(self.projects_dir)
        self.counter = 0

    def _zip(self, entries, name=None):
        self.counter += 1
        path = os.path.join(self.td.name, name or f"t{self.counter}.zip")
        _write_zip(path, entries)
        return path

    def _ingest(self, path, pid=None):
        pid = pid or f"p{self.counter:04d}x"
        return self.ing.ingest_zip(path, pid)

    def _root_of(self, pid):
        return os.path.join(self.projects_dir, pid)

    # -- legitimate behavior intact ------------------------------------
    def test_legit_stored_and_deflated_ingest_byte_exact(self):
        path = os.path.join(self.td.name, "good.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.py", b"import os\n",
                        compress_type=zipfile.ZIP_STORED)
            zf.writestr("sub/b.md", b"# T\n\n- The system must work\n",
                        compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("sub/", b"")
        model = self._ingest(path, "legit1")
        self.assertEqual(sorted(f.path for f in model.files),
                         ["a.py", "sub/b.md"])
        with open(os.path.join(self._root_of("legit1"), "a.py"), "rb") as fh:
            self.assertEqual(fh.read(), b"import os\n")
        self.assertEqual(model.imports.get("a.py"), ["os"])
        self.assertEqual(len(model.requirements), 1)

    def test_legit_unicode_names(self):
        path = self._zip([("ünïcödé-dir/ünïcödé-file.txt",
                           b"unicode paths work")])
        model = self._ingest(path, "legit2")
        self.assertEqual([f.path for f in model.files],
                         ["ünïcödé-dir/ünïcödé-file.txt"])

    def test_encoded_traversal_is_literal_and_contained(self):
        weird = "..%2f..%2fevil.txt"
        path = self._zip([(weird, b"literal")])
        model = self._ingest(path, "legit3")
        self.assertEqual([f.path for f in model.files], [weird])
        disk = os.path.join(self._root_of("legit3"), weird)
        with open(disk, "rb") as fh:
            self.assertEqual(fh.read(), b"literal")
        # nothing escaped the projects dir
        self.assertFalse(os.path.exists(
            os.path.join(self.td.name, "evil.txt")))

    # -- traversal / absolute / windows paths ---------------------------
    def _assert_refused(self, path, pid, *needles):
        with self.assertRaises(ValueError) as ctx:
            self._ingest(path, pid)
        msg = str(ctx.exception)
        for needle in needles:
            self.assertIn(needle, msg, msg)
        return msg

    def test_zipslip_refused(self):
        path = self._zip([("good.txt", b"fine"),
                          ("../../evil.txt", b"pwned")])
        self._assert_refused(path, "m01", "dot-dot")
        self.assertFalse(os.path.exists(
            os.path.join(self.td.name, "evil.txt")))
        self.assertFalse(os.path.exists(self._root_of("m01")))

    def test_nested_zipslip_refused(self):
        path = self._zip([("sub/../../../evil2.txt", b"pwned")])
        self._assert_refused(path, "m02", "dot-dot")
        parent = os.path.dirname(self.td.name)
        self.assertFalse(os.path.exists(os.path.join(parent, "evil2.txt")))

    def test_absolute_path_refused(self):
        path = self._zip([("/abs/evil.txt", b"x")])
        self._assert_refused(path, "m03", "absolute")

    def test_drive_letter_paths_refused(self):
        path = self._zip([("C:/win.txt", b"x")])
        self._assert_refused(path, "m04", "drive-letter")
        path = self._zip([("C:\\win.txt", b"x")])
        self._assert_refused(path, "m04b", "backslash")

    def test_backslash_refused(self):
        path = self._zip([("a\\b.txt", b"x")])
        self._assert_refused(path, "m05", "backslash")

    def test_dot_components_refused(self):
        for name in ("a//b.txt", "./x.txt", "a/./b.txt"):
            path = self._zip([(name, b"x")])
            with self.assertRaises(ValueError, msg=name):
                self._ingest(path, "m06")

    def test_overlong_name_refused(self):
        path = self._zip([("a" * 1025 + ".txt", b"x")])
        self._assert_refused(path, "m07", "too long")

    def test_prefix_confusion_refused(self):
        # The OLD normpath+startswith check had a string-prefix hole: an
        # entry like ../<id>_evil/pwn.txt normalizes to a sibling directory
        # whose path STARTS WITH the root string, so the old check passed
        # it. The canonical name check kills '..' outright.
        pid = "abc123"
        evil_dir = os.path.join(self.projects_dir, pid + "_evil")
        path = self._zip([(f"../{pid}_evil/pwn.txt", b"pwned")])
        self._assert_refused(path, pid, "dot-dot")
        self.assertFalse(os.path.exists(evil_dir))

    # -- symlinks ---------------------------------------------------------
    def test_symlink_entry_never_materialized(self):
        path = self._zip([("real.txt", b"data"),
                          _symlink_info("link", "/etc/passwd"),
                          _symlink_info("sub/link2", "real.txt")])
        self._assert_refused(path, "m08", "symlink")
        out_link = os.path.join(self._root_of("m08"), "link")
        self.assertFalse(os.path.islink(out_link))
        self.assertFalse(os.path.exists(out_link))

    def test_symlink_escape_pair_refused(self):
        outside = os.path.join(self.td.name, "outside")
        os.makedirs(outside)
        sentinel = os.path.join(outside, "sentinel.txt")
        with open(sentinel, "w") as fh:
            fh.write("untouched")
        path = self._zip([_symlink_info("link", outside),
                          ("link/pwned.txt", b"pwned")])
        self._assert_refused(path, "m09", "symlink")
        self.assertFalse(os.path.exists(os.path.join(outside, "pwned.txt")))
        with open(sentinel) as fh:
            self.assertEqual(fh.read(), "untouched")

    # -- bombs / forged sizes ---------------------------------------------
    def test_bomb_overclaim_refused_whole_archive(self):
        path = os.path.join(self.td.name, "bomb.zip")
        _write_zip(path, [("payload.bin", b"tiny")])
        _patch_central_uncompressed_size(path, 4_000_000_000)
        self._assert_refused(path, "m10", "exceeding", "byte limit")
        self.assertFalse(os.path.exists(self._root_of("m10")))

    def test_lying_underclaim_refused(self):
        # Central directory claims 10 bytes; the real stream is 1 MiB.
        # Layering note (observed, not assumed): this environment's zipfile
        # raises BadZipFile on the first read of a size-lied entry, so the
        # stdlib's consistency check fires before our streaming cap — the
        # cap is the second layer (it fires on interpreters without that
        # check, and deterministically on honest oversized streams, see
        # the next test). Either way the archive is refused fail-closed.
        path = os.path.join(self.td.name, "lie.zip")
        big = os.urandom(1024 * 1024)
        _write_zip(path, [("big.bin", big), ("small.txt", b"keep")])
        _patch_central_uncompressed_size(path, 10)
        with self.assertRaises(ValueError):
            self._ingest(path, "m11")
        # partial file removed, no project left behind
        self.assertFalse(os.path.exists(
            os.path.join(self._root_of("m11"), "big.bin")))
        self.assertFalse(os.path.exists(self._root_of("m11")))

    def test_streaming_per_entry_cap_enforced(self):
        # Deterministic proof the streaming cap binds REAL bytes: an honest
        # 1 MiB entry against a 4096-byte per-entry cap. The pre-scan passes
        # (honest 1 MiB claim is under the total cap); the streaming loop
        # refuses mid-entry.
        self.ing.MAX_ENTRY_BYTES = 4096
        path = self._zip([("big.bin", os.urandom(1024 * 1024)),
                          ("small.txt", b"keep")])
        with self.assertRaises(ValueError) as ctx:
            self._ingest(path, "m12")
        self.assertIn("while streaming", str(ctx.exception))
        self.assertIn("4096", str(ctx.exception))
        self.assertFalse(os.path.exists(
            os.path.join(self._root_of("m12"), "big.bin")))
        self.assertFalse(os.path.exists(self._root_of("m12")))

    def test_too_many_entries_refused(self):
        self.ing.MAX_FILES = 5
        path = self._zip([(f"f{i}.txt", b"x") for i in range(6)])
        self._assert_refused(path, "m13", "exceeding", "entry limit")

    def test_encrypted_entry_refused(self):
        # zipfile.writestr() recomputes flag_bits, so the encrypted flag is
        # set by binary patch (same technique as the size patch).
        path = os.path.join(self.td.name, "enc.zip")
        _write_zip(path, [("enc.txt", b"secret"), ("ok.txt", b"fine")])
        _patch_entry_flag_bits(path, "enc.txt", 0x1)
        self._assert_refused(path, "m14", "encrypted")
        self.assertFalse(os.path.exists(self._root_of("m14")))

    # -- masquerade / missing / bad project_id -----------------------------
    def test_tar_gz_masquerade_refused(self):
        tpath = os.path.join(self.td.name, "a.tar.gz")
        with tarfile.open(tpath, "w:gz") as tf:
            info = tarfile.TarInfo("x.txt")
            payload = b"tarred"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        self._assert_refused(tpath, "m15", "not a zip archive")

    def test_missing_archive_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._ingest(os.path.join(self.td.name, "nope.zip"), "m16")
        self.assertIn("not found", str(ctx.exception))

    def test_traversal_project_id_refused(self):
        path = self._zip([("ok.txt", b"fine")])
        for bad in ("../../evil", "/abs", "a/b", "", "x" * 65, "a\x00b",
                    "..", "has space"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.ing.ingest_zip(path, bad)
        self.assertFalse(os.path.exists(
            os.path.join(self.td.name, "evil")))

    # -- unit: name check + containment second layer -------------------------
    def test_entry_name_check_unit(self):
        self.assertFalse(_check_zip_entry_name("../../evil")[0])
        self.assertFalse(_check_zip_entry_name("/abs/x")[0])
        self.assertFalse(_check_zip_entry_name("C:\\win")[0])
        self.assertFalse(_check_zip_entry_name("a\\b")[0])
        self.assertFalse(_check_zip_entry_name("a//b")[0])
        self.assertFalse(_check_zip_entry_name("a\x00b")[0])
        self.assertFalse(_check_zip_entry_name("")[0])
        self.assertTrue(_check_zip_entry_name("a/b/c.txt")[0])
        self.assertTrue(_check_zip_entry_name("ünïcödé/f ✓.txt")[0])
        self.assertTrue(_check_zip_entry_name("sub/")[0])

    def test_contained_target_second_layer(self):
        root_real = os.path.realpath(self.projects_dir)
        with self.assertRaises(ZipEntryUnsafeError):
            ProjectIngestor._contained_target(root_real, "../../x")
        with self.assertRaises(ZipEntryUnsafeError):
            ProjectIngestor._contained_target(root_real, "/abs/x")
        ok = ProjectIngestor._contained_target(root_real, "a/b.txt")
        self.assertTrue(ok.startswith(root_real + os.sep))

    def test_symlink_detector_unit(self):
        zi, _ = _symlink_info("link", "tgt")
        self.assertTrue(_zipinfo_is_symlink(zi))
        zi2 = zipfile.ZipInfo("plain.txt")
        self.assertFalse(_zipinfo_is_symlink(zi2))


if __name__ == "__main__":
    unittest.main()
