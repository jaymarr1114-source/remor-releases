#!/usr/bin/env python3
"""Chat->file routing repair verification.

Tests that chat messages requesting file creation/writing actually
produce files through the governed ScopedFileService path.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from runtime.services.chat_handler import answer
from runtime.services.files import ScopedFileService

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")

def make_services(tmpdir):
    files = ScopedFileService(root=tmpdir, writable=True)
    return {"files": files}

def main():
    global PASS, FAIL
    print("=== chat->file routing ===")
    tmpdir = tempfile.mkdtemp(prefix="chatfile_test_")
    services = make_services(tmpdir)

    # 1. Create a file with explicit content
    r = answer("create a file called hello.txt with content hello world",
               services)
    check("create classified", r["kind"] == "file_create", str(r["kind"]))
    check("create ok", r["mode"] == "answer" and "Created" in r["text"],
          str(r["text"])[:80])
    check("file exists", os.path.isfile(os.path.join(tmpdir, "hello.txt")))
    with open(os.path.join(tmpdir, "hello.txt")) as f:
        check("content correct", f.read() == "hello world")

    # 2. Write content to a file
    r = answer("write second line to notes.txt", services)
    check("write classified", r["kind"] == "file_write", str(r["kind"]))
    check("write ok", "Wrote" in r["text"], str(r["text"])[:80])
    with open(os.path.join(tmpdir, "notes.txt")) as f:
        check("write content", f.read() == "second line")

    # 3. Update an existing file
    r = answer("update the file hello.txt with content goodbye world",
               services)
    check("update classified", r["kind"] == "file_write", str(r["kind"]))
    with open(os.path.join(tmpdir, "hello.txt")) as f:
        check("update content", f.read() == "goodbye world")

    # 4. Append to a file
    r = answer("append more text to notes.txt", services)
    check("append classified", r["kind"] == "file_write", str(r["kind"]))
    with open(os.path.join(tmpdir, "notes.txt")) as f:
        check("append content", f.read() == "second linemore text")

    # 5. Create without content -> clarify (honest, not guessing)
    r = answer("create a file called empty.txt", services)
    check("no-content clarifies", r["mode"] == "clarify",
          f"{r['mode']}/{r['kind']}")

    # 6. Path escape is refused by the governed service
    r = answer("create a file called ../escape.txt with content bad",
               services)
    check("escape refused",
          r["mode"] == "answer" and "Couldn't create" in r["text"],
          str(r["text"])[:80])
    check("no escape file",
          not os.path.isfile(os.path.join(tmpdir, "..", "escape.txt")))

    # 7. Questions about past actions still get the honest "no"
    r = answer("did you delete any files", services)
    check("past-action honest", r["kind"] == "file_action" and
          "no" in r["text"].lower()[:60], str(r["text"])[:60])

    # 8. Missing files service degrades honestly
    r = answer("create a file called x.txt with content y", {})
    check("no-service honest", "isn't available" in r["text"])

    # 9. Grounded provenance present on writes
    r = answer("create a file called prov.txt with content data", services)
    check("grounded present",
          r["grounded"] and r["grounded"].get("store") == "files" and
          r["grounded"].get("path") is not None, str(r["grounded"]))

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
