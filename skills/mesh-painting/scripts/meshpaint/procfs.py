"""Locate the Linux process filesystem that describes this PID namespace."""
from functools import cache
import json
import os
from pathlib import Path
import subprocess


@cache
def mount():
    """Resolve once in the launcher. Both forked workers inherit the result."""
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--list", "--types", "proc", "--output", "TARGET"],
            capture_output=True, check=True, timeout=5,
        )
        entries = json.loads(result.stdout)["filesystems"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        raise RuntimeError("Linux process monitoring requires findmnt from util-linux and a mounted procfs") from error

    for entry in entries:
        root = Path(entry["target"])
        try:
            # A bind mount or a procfs for a different PID namespace may be listed.
            if root.is_absolute() and os.readlink(root / "self") == str(os.getpid()):
                return root
        except OSError:
            continue
    raise RuntimeError("findmnt found no accessible procfs for this process's PID namespace")
