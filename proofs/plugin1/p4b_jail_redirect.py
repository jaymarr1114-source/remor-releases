"""p4b: jail-redirection probe — an absolute-path write to a location
that EXISTS inside the jail (/tmp, a jail-private tmpfs) succeeds from
the plugin's point of view but is neutralized: the host /tmp is
untouched. This proves containment redirects rather than merely
crashing the attacker; the effect dies with the per-run jail."""
from common import FIX, check, fresh_service
import os

svc = fresh_service()
res = svc.register(os.path.join(FIX, "evil_tmp"))
check(res["ok"], "evil_tmp fixture registered")

host_path = "/tmp/jail_redirect_proof.txt"
if os.path.exists(host_path):
    os.remove(host_path)
res = svc.execute("tool.evil_tmp/evil_tmp", {})
check(res.get("ok") is True,
      f"plugin believed its absolute-path write succeeded: {res}")
check(not os.path.isfile(host_path),
      "host /tmp untouched — the write landed in the jail-private "
      "tmpfs and died with the run")
check(res.get("containment", {}).get("enforced") is True,
      "containment posture disclosed on the success path")
print("P4B PASS — absolute write neutralized inside the jail")
