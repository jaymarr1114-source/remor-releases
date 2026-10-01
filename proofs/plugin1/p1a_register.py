"""p1a: register fixtures into the scratch registry (process A)."""
from common import FIX, check, fresh_service
import os

svc = fresh_service()

res = svc.register(os.path.join(FIX, "echo"))
check(res["ok"] and res["entry"]["kind"] == "tool.echo", "echo registered")

res = svc.register(os.path.join(FIX, "dup_a"))
check(res["ok"] and res["entry"]["kind"] == "tool.dup"
      and res["entry"]["name"] == "dup", "tool.dup/dup registered")

res = svc.register(os.path.join(FIX, "dup_b"))
check(res["ok"] and res["entry"]["kind"] == "bot.dup"
      and res["entry"]["name"] == "dup", "bot.dup/dup registered")

listed = svc.list()
names = sorted((p["kind"], p["name"]) for p in listed["plugins"])
check(("tool.echo", "echo") in names, f"registry lists echo: {names}")
check(("tool.dup", "dup") in names, "registry lists tool.dup/dup")
check(("bot.dup", "dup") in names, "registry lists bot.dup/dup")
print("P1A PASS")
