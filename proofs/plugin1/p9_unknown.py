"""p9: unknown refs are typed honest refusals; bare ambiguous names
name their kinds instead of guessing."""
from common import check, fresh_service

svc = fresh_service()

res = svc.execute("nope/missing", {})
check(not res["ok"] and res["error"]["code"] == "PLUGIN_UNKNOWN",
      f"unknown kind/name refused: {res}")

res = svc.execute("", {})
check(not res["ok"] and res["error"]["code"] == "PLUGIN_UNKNOWN",
      f"empty ref refused: {res}")

res = svc.execute("dup", {})
check(not res["ok"] and res["error"]["code"] == "PLUGIN_UNKNOWN",
      f"ambiguous bare name refused: {res}")
kinds = res["error"]["detail"]["kinds"]
check(kinds == ["bot.dup", "tool.dup"], f"kinds listed, not guessed: {kinds}")

# ...while the qualified ref resolves fine
res = svc.execute("bot.dup/dup", {"n": 1})
check(res.get("ok") is True, f"qualified ref resolves: {res}")
print("P9 PASS")
