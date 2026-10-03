"""Pytest path setup for canonical tree tests.

Ensures pylib/ (swarm_engine), distill1/ (governed_student), and the
canonical root are on sys.path so test imports resolve.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# conftest.py is at canonical/tests/conftest.py, so canonical root is 2 up
_CANONICAL = os.path.abspath(os.path.join(_HERE, ".."))

for _p in (
    os.path.join(_CANONICAL, "pylib"),
    os.path.join(_CANONICAL, "distill1"),
    _CANONICAL,
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
