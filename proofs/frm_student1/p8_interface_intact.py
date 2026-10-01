"""P8: server_client.turn's existing interface is unbroken (DEVICE-SIM-1)."""
import os
import sys

TREE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(TREE, "distill1"))
import server_client

text, wall, tps, err = server_client.turn(
    "Chat turn: say hi briefly\nResponse:")
assert err is None, f"direct turn must work: {err}"
assert text, "must return text"
assert wall > 0
print(f"PASS p8_interface_intact: direct turn ok wall={wall:.2f}s")
