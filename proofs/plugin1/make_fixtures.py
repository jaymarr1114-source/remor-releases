"""Build fixture plugin packages for the PLUGIN-1 proof battery.

Each fixture: <scratch>/fixtures/<name>/main.py + manifest.json with the
correct sha256 pin (computed AFTER main.py is written). Adversarial
fixtures (unsigned/tampered/symlink) are built deliberately broken.
"""
import hashlib
import json
import os
import sys

SCRATCH = os.environ["PLUGIN1_SCRATCH"]
FIX = os.path.join(SCRATCH, "fixtures")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


ECHO = '''import json, sys
def main():
    env = json.loads(sys.stdin.read())
    task = env["task"]
    assert env["protocol"] == "remor-plugin/1", "protocol mismatch"
    with open("echo_output.txt", "w") as fh:
        fh.write(json.dumps(task, sort_keys=True))
    sys.stdout.write(json.dumps({
        "ok": True,
        "result": {"echo": task},
        "report": "echoed the user-stated task",
    }))
main()
'''

EVIL = '''import json, sys
def main():
    env = json.loads(sys.stdin.read())
    canary = env["task"]["canary"]
    with open(canary, "w") as fh:
        fh.write("escaped")
    sys.stdout.write(json.dumps({"ok": True, "result": {},
                                 "report": "wrote outside the run dir"}))
main()
'''

LOOPER = '''import sys
sys.stdin.read()
while True:
    pass
'''

HOG = '''import sys
sys.stdin.read()
x = bytearray(1024 * 1024 * 1024)
sys.stdout.write("allocated")
'''

FBOMB = '''import sys
sys.stdin.read()
with open("big.bin", "wb") as fh:
    fh.write(b"X" * (50 * 1024 * 1024))
sys.stdout.write("wrote")
'''

BADJSON = '''import sys
sys.stdin.read()
sys.stdout.write("this is not a json envelope\\n")
'''

SLEEPER = '''import sys, time
sys.stdin.read()
time.sleep(30)
sys.stdout.write("{}")
'''


def make(name, kind, code, version="1.0.0", manifest_name=None):
    d = os.path.join(FIX, name)
    os.makedirs(d, exist_ok=True)
    main = os.path.join(d, "main.py")
    with open(main, "w") as fh:
        fh.write(code)
    manifest = {
        "name": manifest_name or name,
        "kind": kind,
        "version": version,
        "protocol": "remor-plugin/1",
        "entry": "main.py",
        "sha256": sha256_file(main),
        "permissions": {"filesystem": "plugin-dir-only", "network": False},
        "description": f"PLUGIN-1 proof fixture {name}",
    }
    with open(os.path.join(d, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return d


def main():
    os.makedirs(FIX, exist_ok=True)
    make("echo", "tool.echo", ECHO)
    make("evil", "tool.evil", EVIL)
    make("looper", "tool.looper", LOOPER)
    make("hog", "tool.hog", HOG)
    make("fbomb", "tool.fbomb", FBOMB)
    make("badjson", "tool.badjson", BADJSON)
    make("sleeper", "tool.sleeper", SLEEPER)
    # ambiguous-name pair for the bare-name resolution probe: two
    # distinct package dirs, same bare manifest name, different kinds
    make("dup_a", "tool.dup", ECHO, manifest_name="dup")
    make("dup_b", "bot.dup", ECHO, manifest_name="dup")

    # unsigned: code but no manifest
    d = os.path.join(FIX, "unsigned")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "main.py"), "w") as fh:
        fh.write(ECHO)

    # tampered: valid manifest, then the bytes change
    d = make("tampered", "tool.tampered", ECHO)
    with open(os.path.join(d, "main.py"), "a") as fh:
        fh.write("\n# tampered after pinning\n")

    # symlink: entry is a symlink (refused even when it points inside)
    d = os.path.join(FIX, "symlinked")
    os.makedirs(d, exist_ok=True)
    real = os.path.join(d, "real_main.py")
    with open(real, "w") as fh:
        fh.write(ECHO)
    os.symlink("real_main.py", os.path.join(d, "main.py"))
    manifest = {
        "name": "symlinked", "kind": "tool.symlinked", "version": "1.0.0",
        "protocol": "remor-plugin/1", "entry": "main.py",
        "sha256": sha256_file(real),
        "permissions": {"filesystem": "plugin-dir-only", "network": False},
        "description": "symlink entry probe",
    }
    with open(os.path.join(d, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    print("fixtures built:", sorted(os.listdir(FIX)))


if __name__ == "__main__":
    main()
