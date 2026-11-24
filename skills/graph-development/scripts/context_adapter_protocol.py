"""Provider-neutral capsule adapter contract."""

from __future__ import annotations

from typing import Any, Protocol


class CapsuleAdapter(Protocol):
    """Minimum bounded interface implemented by a capsule adapter."""

    def identity(self) -> dict[str, Any]:
        ...

    def context_limit(self) -> int:
        ...

    def count_tokens(self, serialized_payload: str) -> int:
        ...

    def invoke(
        self,
        sealed_envelope: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        ...
