"""Fail closed on Python socket activity during distribution verification."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


LOG_VARIABLE = "GRAPH_DEVELOPMENT_NETWORK_GUARD_LOG"


def _record(kind: str, event: str) -> None:
    raw_path = os.environ.get(LOG_VARIABLE)
    if not raw_path:
        return
    payload = (
        json.dumps(
            {"event": event, "kind": kind, "pid": os.getpid()},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        Path(raw_path),
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def _audit(event: str, _arguments: tuple[object, ...]) -> None:
    if event.startswith("socket."):
        _record("denied", event)
        raise RuntimeError(
            f"network access denied by Graph Development verification: {event}"
        )


_record("loaded", "sitecustomize")
sys.addaudithook(_audit)
