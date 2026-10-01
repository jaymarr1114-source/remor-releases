"""p1b: fresh process — the registry survived process death (disk, not memory)."""
from common import check, fresh_service

svc = fresh_service()
listed = svc.list()
names = sorted((p["kind"], p["name"]) for p in listed["plugins"])
check(("tool.echo", "echo") in names, f"echo persisted: {names}")
check(("tool.dup", "dup") in names, "tool.dup persisted")
check(("bot.dup", "dup") in names, "bot.dup persisted")
entry = [p for p in listed["plugins"] if p["kind"] == "tool.echo"][0]
check(len(entry["sha256"]) == 64, "pin persisted with entry")
print("P1B PASS — registry is disk-persisted across processes")
