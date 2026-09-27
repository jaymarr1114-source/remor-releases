"""Single writable-root resolution for all filesystem writes.

On Android (Chaquopy), the app-private files dir is the only writable
location. The Android wrapper (remor_android.py) sets REMOR_DATA_DIR to
<payload>/.remor_gui at startup, before any engine code runs.

On dev machines REMOR_DATA_DIR is unset and callers fall back to their
historical defaults (typically /tmp/...), preserving existing behavior.

Usage:
    from swarm_engine.core.writable import writable_subdir
    projects_dir = writable_subdir("swarm_projects", "/tmp/swarm_projects")
"""

import os
import tempfile


def writable_root():
    """Return the configured writable root, or None if not configured."""
    return os.environ.get("REMOR_DATA_DIR")


def writable_subdir(name, fallback=None):
    """Return <writable_root>/<name>, or fallback if no root is configured.

    If fallback is None and no root is configured, uses the system temp
    dir (which respects TMPDIR).
    """
    root = writable_root()
    if root:
        return os.path.join(root, name)
    if fallback:
        return fallback
    return os.path.join(tempfile.gettempdir(), name)
