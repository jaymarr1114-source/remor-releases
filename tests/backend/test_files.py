"""Causal tests for swarm_engine.services.files.ScopedFileService.

Real filesystem attacks: traversal strings, absolute paths, and symlinks
planted on disk. Every refusal below is executed against a real tmp dir.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.files import ScopedFileService  # noqa: E402


def _svc_tree(writable=False):
    td = tempfile.TemporaryDirectory()
    root = td.name
    os.makedirs(os.path.join(root, "sub", "deep"))
    with open(os.path.join(root, "hello.txt"), "w") as fh:
        fh.write("hello world")
    with open(os.path.join(root, "sub", "deep", "note.txt"), "w") as fh:
        fh.write("deep note")
    return td, ScopedFileService(root, writable=writable)


class TestTraversalRefusals(unittest.TestCase):
    def setUp(self):
        self.td, self.svc = _svc_tree()

    def tearDown(self):
        self.td.cleanup()

    def _refused_everywhere(self, rel):
        # read paths must be refused for the escape reason itself
        for op in (
            lambda: self.svc.read_text(rel),
            lambda: self.svc.list_dir(rel),
            lambda: self.svc.stat(rel),
        ):
            r = op()
            self.assertFalse(r["ok"], f"{rel} not refused")
            self.assertTrue(
                "root" in r["error"] or "absolute" in r["error"],
                f"unexpected refusal reason for {rel}: {r['error']}",
            )
        # writes on this (read-only) service are refused regardless of path;
        # a writable service must ALSO refuse them for the escape reason.
        for op in (
            lambda: self.svc.write_text(rel, "x"),
            lambda: self.svc.mkdir(rel),
        ):
            r = op()
            self.assertFalse(r["ok"], f"{rel} write not refused")
            self.assertEqual(r["error"], "read-only scope")
        wsvc = ScopedFileService(os.path.realpath(self.td.name), writable=True)
        for op in (
            lambda: wsvc.write_text(rel, "x"),
            lambda: wsvc.mkdir(rel),
        ):
            r = op()
            self.assertFalse(r["ok"], f"{rel} write not refused (writable)")
            self.assertTrue(
                "root" in r["error"] or "absolute" in r["error"],
                f"unexpected write refusal for {rel}: {r['error']}",
            )

    def test_dotdot_refused(self):
        self._refused_everywhere("../evil")

    def test_abs_path_refused(self):
        self._refused_everywhere("/abs/path")

    def test_deep_traversal_refused(self):
        self._refused_everywhere("a/../../evil")
        self._refused_everywhere("sub/../../..")

    def test_nul_byte_refused(self):
        r = self.svc.read_text("a\x00b")
        self.assertFalse(r["ok"])


class TestSymlinks(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.outside = tempfile.TemporaryDirectory()
        with open(os.path.join(self.outside.name, "secret.txt"), "w") as fh:
            fh.write("TOP SECRET")
        with open(os.path.join(self.root, "inner.txt"), "w") as fh:
            fh.write("inner")
        # escape: symlink inside root -> target outside root
        os.symlink(
            os.path.join(self.outside.name, "secret.txt"),
            os.path.join(self.root, "escape_link"),
        )
        os.symlink(self.outside.name, os.path.join(self.root, "escape_dir"))
        # inside: symlink inside root -> target inside root
        os.symlink("inner.txt", os.path.join(self.root, "inside_link"))
        os.symlink("sub", os.path.join(self.root, "inside_dir_link"))
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "f.txt"), "w") as fh:
            fh.write("subfile")
        self.svc = ScopedFileService(self.root, writable=True)

    def tearDown(self):
        self.td.cleanup()
        self.outside.cleanup()

    def test_escape_symlink_read_refused(self):
        r = self.svc.read_text("escape_link")
        self.assertFalse(r["ok"])
        self.assertNotIn("TOP SECRET", str(r))

    def test_escape_symlink_write_refused(self):
        r = self.svc.write_text("escape_link", "pwned")
        self.assertFalse(r["ok"])
        # outside file untouched
        with open(os.path.join(self.outside.name, "secret.txt")) as fh:
            self.assertEqual(fh.read(), "TOP SECRET")

    def test_escape_dir_list_refused(self):
        r = self.svc.list_dir("escape_dir")
        self.assertFalse(r["ok"])

    def test_escape_symlink_stat_refused(self):
        r = self.svc.stat("escape_dir/../escape_link")
        self.assertFalse(r["ok"])

    def test_inside_symlink_read_works(self):
        r = self.svc.read_text("inside_link")
        self.assertTrue(r["ok"])
        self.assertEqual(r["content"], "inner")

    def test_inside_symlink_dir_list_works(self):
        r = self.svc.list_dir("inside_dir_link")
        self.assertTrue(r["ok"])
        self.assertEqual([e["name"] for e in r["entries"]], ["f.txt"])


class TestContentRules(unittest.TestCase):
    def setUp(self):
        self.td, self.svc = _svc_tree()

    def tearDown(self):
        self.td.cleanup()

    def test_binary_read_refused(self):
        p = os.path.join(self.td.name, "blob.bin")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x01\x02binary\xff")
        r = self.svc.read_text("blob.bin")
        self.assertFalse(r["ok"])
        self.assertIn("binary", r["error"].lower())

    def test_oversize_refused(self):
        p = os.path.join(self.td.name, "big.txt")
        with open(p, "w") as fh:
            fh.write("x" * 300_000)
        r = self.svc.read_text("big.txt", max_bytes=200_000)
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])

    def test_read_dir_refused(self):
        r = self.svc.read_text("sub")
        self.assertFalse(r["ok"])

    def test_list_dir_dirs_first(self):
        r = self.svc.list_dir("")
        self.assertTrue(r["ok"])
        entries = r["entries"]
        self.assertEqual(entries[0]["name"], "sub")
        self.assertTrue(entries[0]["is_dir"])
        names = [e["name"] for e in entries]
        self.assertIn("hello.txt", names)

    def test_stat_file(self):
        r = self.svc.stat("hello.txt")
        self.assertTrue(r["ok"])
        self.assertFalse(r["is_dir"])
        self.assertEqual(r["size_bytes"], 11)


class TestReadOnlyAndNoLeak(unittest.TestCase):
    def test_read_only_refuses_writes(self):
        td, svc = _svc_tree(writable=False)
        try:
            for op in (
                lambda: svc.write_text("new.txt", "x"),
                lambda: svc.mkdir("newdir"),
            ):
                r = op()
                self.assertFalse(r["ok"])
                self.assertEqual(r["error"], "read-only scope")
            self.assertFalse(os.path.exists(os.path.join(td.name, "new.txt")))
            self.assertFalse(os.path.exists(os.path.join(td.name, "newdir")))
        finally:
            td.cleanup()

    def test_writable_roundtrip(self):
        td, svc = _svc_tree(writable=True)
        try:
            w = svc.write_text("sub/new.txt", "round trip")
            self.assertTrue(w["ok"])
            r = svc.read_text("sub/new.txt")
            self.assertTrue(r["ok"])
            self.assertEqual(r["content"], "round trip")
            m = svc.mkdir("a/b")
            self.assertTrue(m["ok"])
            self.assertTrue(os.path.isdir(os.path.join(td.name, "a", "b")))
        finally:
            td.cleanup()

    def test_no_absolute_root_leak(self):
        td, svc = _svc_tree()
        try:
            root = os.path.realpath(td.name)
            results = [
                svc.read_text("../evil"),
                svc.read_text("nope.txt"),
                svc.read_text("blob-missing"),
                svc.list_dir(""),
                svc.stat("hello.txt"),
                svc.write_text("x.txt", "y"),
            ]
            blob = str(results)
            self.assertNotIn(root, blob, "absolute root leaked in a response")
            # also no partial path component beyond the scope name
            for r in results:
                if r.get("ok") and "entries" in r:
                    for e in r["entries"]:
                        self.assertFalse(os.path.isabs(e["rel"]))
                        self.assertFalse(e["rel"].startswith(".."))
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
