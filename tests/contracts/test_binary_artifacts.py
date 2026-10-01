"""Causal tests for swarm_engine.services.binary_artifacts (Contract 6).

Real sqlite + real blob files on scratch dirs. Revert-style checks:
tamper the bytes on disk, delete the blob, shrink the size guard — the
contract must refuse or enforce, proving the checks are real.
"""
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402
from swarm_engine.services.binary_artifacts import (  # noqa: E402
    BinaryArtifactStore, MAX_BINARY_BYTES, routes_for_binary_artifacts)
from swarm_engine.services import binary_artifacts as ba_mod  # noqa: E402
from swarm_engine.services.contract_types import dispatch  # noqa: E402


def _stores():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "artifacts.db")
    sandbox = os.path.join(td.name, "sandbox")
    store = ArtifactStore(db, sandbox)
    blobs = BinaryArtifactStore(store)
    return td, store, blobs


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hello.txt", "hello-binary-world")
        zf.writestr("dir/nested.bin", bytes(range(256)))
    return buf.getvalue()


def _wav_bytes(freq=440.0, seconds=1.0, sample_rate=22050, stereo=False):
    """Tiny real PCM WAV (sine tone) built with the stdlib only."""
    import math
    import struct
    import wave
    n = int(seconds * sample_rate)
    mono = [int(16000 * math.sin(2.0 * math.pi * freq * i / sample_rate))
            for i in range(n)]
    if stereo:
        frames = struct.pack("<%dh" % (2 * n),
                             *[s for m in mono for s in (m, m)])
        nch = 2
    else:
        frames = struct.pack("<%dh" % n, *mono)
        nch = 1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(frames)
    return buf.getvalue()


class TestBinaryRoundtrip(unittest.TestCase):
    def test_zip_bytes_in_byte_identical_out(self):
        td, store, blobs = _stores()
        try:
            payload = _zip_bytes()
            saved = blobs.store_file("bundle.zip", payload,
                                     "application/zip")
            self.assertTrue(saved["ok"], saved)
            self.assertEqual(saved["kind"], "binary")
            self.assertEqual(saved["size_bytes"], len(payload))
            self.assertEqual(saved["sha256"],
                             hashlib.sha256(payload).hexdigest())
            got = blobs.get_file(saved["artifact_id"])
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["data"], payload)  # byte-identical
            self.assertEqual(got["content_type"], "application/zip")
            self.assertEqual(got["name"], "bundle.zip")
        finally:
            td.cleanup()

    def test_any_file_type_opaque(self):
        # Blender-style binary + tarball-style bytes: opaque, no sniffing.
        td, store, blobs = _stores()
        try:
            fake_blend = b"BLENDER" + bytes(range(256)) * 40
            s1 = blobs.store_file("scene.blend", fake_blend,
                                  "application/x-blender")
            self.assertTrue(s1["ok"])
            g1 = blobs.get_file(s1["artifact_id"])
            self.assertEqual(g1["data"], fake_blend)
            self.assertEqual(g1["content_type"], "application/x-blender")
        finally:
            td.cleanup()

    def test_empty_bytes_roundtrip(self):
        td, store, blobs = _stores()
        try:
            saved = blobs.store_file("empty.bin", b"", "application/octet-stream")
            self.assertTrue(saved["ok"], saved)
            got = blobs.get_file(saved["artifact_id"])
            self.assertTrue(got["ok"])
            self.assertEqual(got["data"], b"")
            self.assertEqual(got["size_bytes"], 0)
        finally:
            td.cleanup()


class TestTamperRefused(unittest.TestCase):
    def test_tampered_blob_refused(self):
        td, store, blobs = _stores()
        try:
            payload = _zip_bytes()
            saved = blobs.store_file("t.zip", payload, "application/zip")
            aid = saved["artifact_id"]
            sha = saved["sha256"]
            bpath = os.path.join(blobs._blob_dir, sha[:2], sha)
            # Tamper: flip a byte in the middle of the blob on disk.
            with open(bpath, "r+b") as fh:
                fh.seek(len(payload) // 2)
                byte = fh.read(1)
                fh.seek(len(payload) // 2)
                fh.write(bytes([byte[0] ^ 0xFF]))
            got = blobs.get_file(aid)
            self.assertFalse(got["ok"])
            self.assertIn("integrity check failed", got["error"])
        finally:
            td.cleanup()

    def test_missing_blob_reported_not_empty(self):
        td, store, blobs = _stores()
        try:
            saved = blobs.store_file("m.zip", _zip_bytes(), "application/zip")
            aid = saved["artifact_id"]
            sha = saved["sha256"]
            os.remove(os.path.join(blobs._blob_dir, sha[:2], sha))
            got = blobs.get_file(aid)
            self.assertFalse(got["ok"])
            self.assertIn("blob missing", got["error"])
        finally:
            td.cleanup()

    def test_unknown_id_not_found(self):
        td, store, blobs = _stores()
        try:
            got = blobs.get_file(424242)
            self.assertFalse(got["ok"])
            self.assertIn("not found", got["error"])
        finally:
            td.cleanup()


class TestOversizeGuard(unittest.TestCase):
    def test_limit_constant_is_200mib(self):
        self.assertEqual(MAX_BINARY_BYTES, 200 * 1024 * 1024)

    def test_oversize_refused_before_write(self):
        td, store, blobs = _stores()
        old = ba_mod.MAX_BINARY_BYTES
        try:
            ba_mod.MAX_BINARY_BYTES = 64  # shrink for the test; module reads it live
            big = b"x" * 65
            res = blobs.store_file("big.bin", big)
            self.assertFalse(res["ok"])
            self.assertIn("oversize", res["error"])
            # Boundary: exactly at the limit passes.
            edge = blobs.store_file("edge.bin", b"x" * 64)
            self.assertTrue(edge["ok"], edge)
            # Nothing was written for the refused upload.
            self.assertEqual(
                [b["name"] for b in blobs.list_binaries()], ["edge.bin"])
        finally:
            ba_mod.MAX_BINARY_BYTES = old
            td.cleanup()

    def test_non_bytes_refused(self):
        td, store, blobs = _stores()
        try:
            res = blobs.store_file("s.txt", "not-bytes")
            self.assertFalse(res["ok"])
            self.assertIn("bytes", res["error"])
        finally:
            td.cleanup()


class TestNameValidation(unittest.TestCase):
    def test_hostile_names_refused(self):
        td, store, blobs = _stores()
        try:
            for bad in ("", "   ", "../evil", "a/b", "a\\b",
                        "x\x00y", ".", "..", "x" * 256):
                res = blobs.store_file(bad, b"data")
                self.assertFalse(res["ok"], repr(bad))
            self.assertEqual(blobs.list_binaries(), [])
        finally:
            td.cleanup()

    def test_unicode_name_ok(self):
        td, store, blobs = _stores()
        try:
            res = blobs.store_file("séance-mix.wav", b"RIFF....",
                                   "audio/wav")
            self.assertTrue(res["ok"], res)
            got = blobs.get_file(res["artifact_id"])
            self.assertEqual(got["name"], "séance-mix.wav")
        finally:
            td.cleanup()


class TestDedupeAndDelete(unittest.TestCase):
    def test_identical_bytes_dedupe_one_blob(self):
        td, store, blobs = _stores()
        try:
            payload = _zip_bytes()
            s1 = blobs.store_file("a.zip", payload, "application/zip")
            s2 = blobs.store_file("b.zip", payload, "application/zip")
            self.assertNotEqual(s1["artifact_id"], s2["artifact_id"])
            self.assertEqual(s1["sha256"], s2["sha256"])
            sha = s1["sha256"]
            shard = os.path.join(blobs._blob_dir, sha[:2])
            self.assertEqual(os.listdir(shard), [sha])  # one blob file
            # Delete one name: the bytes stay for the other.
            self.assertTrue(blobs.delete_binary(s1["artifact_id"])["ok"])
            self.assertTrue(os.path.exists(os.path.join(shard, sha)))
            g2 = blobs.get_file(s2["artifact_id"])
            self.assertTrue(g2["ok"])
            self.assertEqual(g2["data"], payload)
            # Delete the last reference: blob file goes away.
            self.assertTrue(blobs.delete_binary(s2["artifact_id"])["ok"])
            self.assertFalse(os.path.exists(os.path.join(shard, sha)))
            self.assertFalse(blobs.get_file(s2["artifact_id"])["ok"])
        finally:
            td.cleanup()

    def test_delete_unknown(self):
        td, store, blobs = _stores()
        try:
            self.assertFalse(blobs.delete_binary(999)["ok"])
        finally:
            td.cleanup()


class TestListAll(unittest.TestCase):
    def test_all_artifacts_shows_code_and_binary(self):
        td, store, blobs = _stores()
        try:
            code = store.save("script", "python", "print(1)")
            self.assertTrue(code["ok"])
            bin1 = blobs.store_file("bundle.zip", _zip_bytes(),
                                    "application/zip")
            self.assertTrue(bin1["ok"])
            all_items = blobs.list_all()
            by_kind = {}
            for item in all_items:
                by_kind.setdefault(item["kind"], []).append(item)
            self.assertEqual(len(by_kind["code"]), 1)
            self.assertEqual(len(by_kind["binary"]), 1)
            self.assertEqual(by_kind["code"][0]["artifact_id"],
                             code["artifact_id"])
            self.assertEqual(by_kind["binary"][0]["artifact_id"],
                             bin1["artifact_id"])
            self.assertEqual(by_kind["binary"][0]["content_type"],
                             "application/zip")
            json.dumps({"ok": True, "artifacts": all_items})
        finally:
            td.cleanup()


class TestAudioMixReal(unittest.TestCase):
    """POST /api/artifacts/audio/mix is REAL (AUDIO-1): stems stored as
    binary artifacts are decoded, mixed, and stored as a song artifact."""

    def _stems(self, blobs):
        inst = blobs.store_file("inst.wav",
                                _wav_bytes(220.0, 2.0, 22050, stereo=True),
                                "audio/wav")
        v1 = blobs.store_file("v1.wav", _wav_bytes(440.0, 1.0), "audio/wav")
        v2 = blobs.store_file("v2.wav", _wav_bytes(660.0, 1.5), "audio/wav")
        self.assertTrue(inst["ok"] and v1["ok"] and v2["ok"])
        return inst["artifact_id"], [v1["artifact_id"], v2["artifact_id"]]

    def test_mix_audio_real(self):
        import struct
        import wave
        td, store, blobs = _stores()
        try:
            iid, vids = self._stems(blobs)
            res = blobs.mix_audio(instrumental_id=iid, voice_ids=vids)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["content_type"], "audio/wav")
            self.assertEqual(res["n_voices"], 2)
            # honestly labeled: ACE-Step is NOT the instrumental engine
            self.assertIn("ACE-Step NOT", res["engines"]["instrumental"])
            got = blobs.get_file(res["artifact_id"])
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["sha256"], res["sha256"])  # recorded
            self.assertEqual(hashlib.sha256(got["data"]).hexdigest(),
                             res["sha256"])
            # byte-real song: decodes as stereo WAV, non-silent, sane length
            with wave.open(io.BytesIO(got["data"]), "rb") as w:
                self.assertEqual(w.getnchannels(), 2)
                self.assertEqual(w.getframerate(), 44100)
                raw = w.readframes(w.getnframes())
            samps = struct.unpack("<%dh" % (len(raw) // 2), raw)
            self.assertGreater(max(abs(s) for s in samps), 1000)
            dur = len(samps) // 2 / 44100
            self.assertAlmostEqual(dur, 2.0, delta=0.15)
            # listed among artifacts (artifact lab can serve it)
            ids = [a["artifact_id"] for a in blobs.list_all()]
            self.assertIn(res["artifact_id"], ids)
            json.dumps(res)
        finally:
            td.cleanup()

    def test_mix_audio_refusals(self):
        td, store, blobs = _stores()
        try:
            iid, vids = self._stems(blobs)
            # unknown artifact id
            r = blobs.mix_audio(instrumental_id=999999, voice_ids=vids)
            self.assertFalse(r["ok"])
            # non-audio bytes stored as an artifact
            txt = blobs.store_file("note.txt", b"not audio at all",
                                   "text/plain")
            r = blobs.mix_audio(instrumental_id=txt["artifact_id"],
                                voice_ids=vids)
            self.assertFalse(r["ok"])
            self.assertIn("decod", r["error"].lower())
            # empty / malformed ids
            self.assertFalse(blobs.mix_audio(instrumental_id=iid,
                                             voice_ids=[])["ok"])
            self.assertFalse(blobs.mix_audio(instrumental_id="x",
                                             voice_ids=vids)["ok"])
            self.assertFalse(blobs.mix_audio(instrumental_id=True,
                                             voice_ids=vids)["ok"])
        finally:
            td.cleanup()


class TestRoutes(unittest.TestCase):
    def test_upload_download_roundtrip_over_routes(self):
        td, store, blobs = _stores()
        try:
            routes = routes_for_binary_artifacts(blobs)
            payload = _zip_bytes()
            up = dispatch(routes, "POST", "/api/artifacts/binary", {
                "name": "route.zip",
                "content_type": "application/zip",
                "bytes_b64": base64.b64encode(payload).decode("ascii"),
            })
            self.assertTrue(up["ok"], up)
            aid = up["artifact_id"]
            json.dumps(up)
            down = dispatch(routes, "GET",
                            f"/api/artifacts/binary/{aid}", {})
            self.assertTrue(down["ok"], down)
            dl = down["download"]
            self.assertEqual(dl["artifact_id"], aid)
            self.assertEqual(dl["name"], "route.zip")
            self.assertEqual(dl["content_type"], "application/zip")
            self.assertEqual(dl["sha256"],
                             hashlib.sha256(payload).hexdigest())
            self.assertEqual(dl["size_bytes"], len(payload))
            self.assertEqual(base64.b64decode(dl["bytes"]), payload)
            json.dumps(down)  # stays JSON-serializable
        finally:
            td.cleanup()

    def test_upload_bad_base64_refused(self):
        td, store, blobs = _stores()
        try:
            routes = routes_for_binary_artifacts(blobs)
            res = dispatch(routes, "POST", "/api/artifacts/binary",
                           {"name": "x.bin", "bytes_b64": "!!!not-base64!!!"})
            self.assertFalse(res["ok"])
            json.dumps(res)
        finally:
            td.cleanup()

    def test_list_all_route(self):
        td, store, blobs = _stores()
        try:
            routes = routes_for_binary_artifacts(blobs)
            store.save("s", "python", "print(1)")
            blobs.store_file("b.bin", b"zz")
            res = dispatch(routes, "GET", "/api/artifacts/all", {})
            self.assertTrue(res["ok"])
            kinds = sorted(i["kind"] for i in res["artifacts"])
            self.assertEqual(kinds, ["binary", "code"])
            json.dumps(res)
        finally:
            td.cleanup()

    def test_delete_route(self):
        td, store, blobs = _stores()
        try:
            routes = routes_for_binary_artifacts(blobs)
            saved = blobs.store_file("d.bin", b"data")
            res = dispatch(routes, "DELETE",
                           f"/api/artifacts/binary/{saved['artifact_id']}",
                           {})
            self.assertTrue(res["ok"], res)
            self.assertFalse(
                blobs.get_file(saved["artifact_id"])["ok"])
        finally:
            td.cleanup()

    def test_audio_mix_route_real(self):
        td, store, blobs = _stores()
        try:
            routes = routes_for_binary_artifacts(blobs)
            inst = blobs.store_file("inst.wav",
                                    _wav_bytes(220.0, 1.0, 22050,
                                               stereo=True), "audio/wav")
            v1 = blobs.store_file("v1.wav", _wav_bytes(440.0, 1.0),
                                  "audio/wav")
            res = dispatch(routes, "POST", "/api/artifacts/audio/mix",
                           {"instrumental_id": inst["artifact_id"],
                            "voice_ids": [v1["artifact_id"]]})
            self.assertTrue(res["ok"], res)
            self.assertIn("artifact_id", res)
            # the song is retrievable over the download route, byte-identical
            dl = dispatch(
                routes, "GET",
                "/api/artifacts/binary/%d" % res["artifact_id"], {})
            self.assertTrue(dl["ok"], dl)
            self.assertEqual(dl["download"]["sha256"], res["sha256"])
            raw = base64.b64decode(dl["download"]["bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),
                             res["sha256"])
            json.dumps(res)
        finally:
            td.cleanup()

    def test_download_tampered_refused_over_route(self):
        td, store, blobs = _stores()
        try:
            routes = routes_for_binary_artifacts(blobs)
            payload = _zip_bytes()
            saved = blobs.store_file("t.zip", payload, "application/zip")
            aid, sha = saved["artifact_id"], saved["sha256"]
            bpath = os.path.join(blobs._blob_dir, sha[:2], sha)
            with open(bpath, "r+b") as fh:
                fh.seek(10)
                b = fh.read(1)
                fh.seek(10)
                fh.write(bytes([b[0] ^ 1]))
            res = dispatch(routes, "GET", f"/api/artifacts/binary/{aid}", {})
            self.assertFalse(res["ok"])
            self.assertIn("integrity check failed", res["error"])
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
