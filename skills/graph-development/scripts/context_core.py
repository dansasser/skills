"""Deterministic, provider-neutral context contracts for Graph Development V3."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Any, Protocol


SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
SECRET_MARKER = re.compile(
    r"(?:sk|ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{12,}",
    re.IGNORECASE,
)
ENVELOPE_FIELDS = {
    "schema_version",
    "envelope_id",
    "mode",
    "graph",
    "task_node_id",
    "execution_context",
    "budget",
    "manifest",
    "payload",
    "selection",
    "request_policy",
    "parent_request_digest",
    "digest",
}
REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "envelope_id",
    "task_node_id",
    "kind",
    "purpose",
    "max_tokens",
    "requested_artifacts",
    "capability",
    "arguments",
    "digest",
}
RESULT_FIELDS = {
    "schema_version",
    "envelope_id",
    "task_node_id",
    "status",
    "output_artifacts",
    "result_payload",
    "request",
    "usage",
    "provenance",
    "error",
    "digest",
}
RESULT_STATUSES = {
    "completed",
    "context_request",
    "action_request",
    "mandatory_overflow",
    "policy_denied",
    "invalid_envelope",
    "adapter_failure",
    "cancelled",
}


class ContextValidationError(ValueError):
    """Raised when a V3 context contract is invalid."""

    def __init__(self, errors: list[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


class ContextControlError(ValueError):
    """Raised when safe context planning cannot proceed."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        artifact_id: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.artifact_id = artifact_id
        super().__init__(message)


class Tokenizer(Protocol):
    tokenizer_id: str
    exact: bool

    def count_tokens(self, text: str) -> int:
        """Return the exact token count for the supplied text."""


class DeterministicWordTokenizer:
    """Small exact tokenizer for the deterministic fake adapter and tests."""

    exact = True

    def __init__(self, tokenizer_id: str = "deterministic-word-v1") -> None:
        self.tokenizer_id = tokenizer_id

    def count_tokens(self, text: str) -> int:
        return len(re.findall(r"\S+", text))


class PreviewTokenizer:
    """Explicitly approximate tokenizer that can never authorize execution."""

    exact = False
    tokenizer_id = "preview-utf8-div4-v1"

    def count_tokens(self, text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / 4)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON value canonically for byte equality and hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def seal_document(value: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-only copy with a digest over every other field."""

    sealed = copy.deepcopy(value)
    sealed.pop("digest", None)
    sealed["digest"] = sha256_json(sealed)
    return sealed


def _secret_errors(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, str):
        if SECRET_MARKER.search(value):
            errors.append(f"{path}: contains a credential-like secret marker")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_secret_errors(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            errors.extend(_secret_errors(item, f"{path}.{key}"))
    return errors


MANDATORY_CLASS_ORDER = {
    "node_contract": 0,
    "direct_input": 1,
    "policy": 2,
    "authority": 3,
    "approval": 4,
    "safety": 5,
    "invalidation": 6,
    "capability": 7,
}
OPTIONAL_OMISSION_REASONS = frozenset(
    {
        "unauthorized",
        "stale",
        "provenance-incomplete",
        "conflict",
        "no-coverage",
        "no-positive-coverage",
        "redundant",
        "budget",
        "not-selected",
        "serialization-overflow",
    }
)
RESULT_KEY_FIELDS = {
    "graph_checksum",
    "checkpoint",
    "node_contract_digest",
    "implementation_version",
    "direct_input_versions",
    "envelope_digest",
    "selector_version",
    "compactor_version",
    "policy_versions",
    "approval_versions",
    "adapter_identity",
    "tokenizer_identity",
    "runtime_config",
}


def _context_item(
    raw: dict[str, Any],
    *,
    inclusion_class: str,
) -> dict[str, Any]:
    required = {"artifact_id", "content", "source", "requirements"}
    missing = sorted(required - set(raw))
    if missing:
        raise ContextControlError(
            f"context item missing fields: {', '.join(missing)}"
        )
    artifact_id = raw["artifact_id"]
    content = raw["content"]
    source = raw["source"]
    requirements = raw["requirements"]
    version = raw.get("version", "unversioned")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ContextControlError("context item artifact_id must be nonempty")
    if not isinstance(content, str):
        raise ContextControlError(f"{artifact_id} content must be a string")
    path_errors: list[str] = []
    if not _portable_source(source, f"{artifact_id}.source", path_errors):
        raise ContextControlError("; ".join(path_errors))
    if not isinstance(requirements, list) or not all(
        isinstance(item, str) and item for item in requirements
    ):
        raise ContextControlError(
            f"{artifact_id} requirements must be nonempty strings"
        )
    if not isinstance(version, str) or not version.strip():
        raise ContextControlError(
            f"{artifact_id} version must be a nonempty string"
        )
    if inclusion_class not in MANDATORY_CLASS_ORDER and inclusion_class != "optional":
        raise ContextControlError(
            f"{artifact_id} has unsupported inclusion class {inclusion_class!r}"
        )
    return {
        "artifact_id": artifact_id,
        "content": content,
        "source": source,
        "requirements": sorted(set(requirements)),
        "inclusion_class": inclusion_class,
        "authorized": raw.get("authorized", True),
        "fresh": raw.get("fresh", True),
        "provenance_complete": raw.get("provenance_complete", True),
        "conflicting": raw.get("conflicting", False),
        "version": version,
    }


def _mandatory_item(raw: dict[str, Any]) -> dict[str, Any]:
    inclusion_class = raw.get("inclusion_class", "direct_input")
    item = _context_item(raw, inclusion_class=inclusion_class)
    if item["authorized"] is not True:
        raise ContextControlError(
            f"mandatory artifact {item['artifact_id']} is not authorized",
            reason_code="unauthorized",
            artifact_id=item["artifact_id"],
        )
    if item["fresh"] is not True:
        raise ContextControlError(
            f"mandatory artifact {item['artifact_id']} is not fresh",
            reason_code="stale",
            artifact_id=item["artifact_id"],
        )
    if item["provenance_complete"] is not True:
        raise ContextControlError(
            f"mandatory artifact {item['artifact_id']} lacks provenance",
            reason_code="provenance-incomplete",
            artifact_id=item["artifact_id"],
        )
    if item["conflicting"] is True:
        raise ContextControlError(
            f"mandatory artifact {item['artifact_id']} conflicts with current state",
            reason_code="conflict",
            artifact_id=item["artifact_id"],
        )
    return item


def _node_contract_item(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    matches = [
        node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("id") == node_id
    ]
    if len(matches) != 1:
        raise ContextControlError(f"unknown or duplicate task node: {node_id}")
    node = matches[0]
    contract = {
        field: node.get(field)
        for field in (
            "id",
            "goal",
            "executor",
            "inputs",
            "outputs",
            "acceptance",
            "permissions",
            "execution",
            "side_effects",
        )
    }
    return _mandatory_item(
        {
            "artifact_id": f"node-contract:{node_id}",
            "content": canonical_json_bytes(contract).decode("utf-8"),
            "source": "development-graph.json",
            "requirements": [
                item.get("id", f"acceptance-{index}")
                for index, item in enumerate(node.get("acceptance", []))
                if isinstance(item, dict)
            ],
            "inclusion_class": "node_contract",
            "version": sha256_json(contract),
        }
    )


def build_mandatory_frontier(
    graph: dict[str, Any],
    task_node_id: str,
    artifact_registry: dict[str, dict[str, Any]],
    *,
    extras: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve the node contract, required direct inputs, and mandatory closure."""

    frontier = [_node_contract_item(graph, task_node_id)]
    node = next(
        node
        for node in graph["nodes"]
        if isinstance(node, dict) and node.get("id") == task_node_id
    )
    for input_contract in node.get("inputs", []):
        if not isinstance(input_contract, dict) or not input_contract.get("required"):
            continue
        artifact_id = input_contract.get("artifact")
        if not isinstance(artifact_id, str):
            raise ContextControlError(
                f"{task_node_id} has a malformed required input"
            )
        raw = artifact_registry.get(artifact_id)
        if raw is None:
            raise ContextControlError(
                f"required artifact is unavailable: {artifact_id}",
                reason_code="unavailable",
                artifact_id=artifact_id,
            )
        candidate = dict(raw)
        candidate["artifact_id"] = artifact_id
        candidate["inclusion_class"] = "direct_input"
        frontier.append(_mandatory_item(candidate))
    frontier.extend(_mandatory_item(dict(item)) for item in (extras or []))
    identifiers = [item["artifact_id"] for item in frontier]
    duplicates = sorted(
        identifier
        for identifier in set(identifiers)
        if identifiers.count(identifier) > 1
    )
    if duplicates:
        raise ContextControlError(
            "duplicate mandatory artifacts: " + ", ".join(duplicates)
        )
    return sorted(
        frontier,
        key=lambda item: (
            MANDATORY_CLASS_ORDER[item["inclusion_class"]],
            item["artifact_id"],
        ),
    )


def _optional_eligibility(item: dict[str, Any]) -> str | None:
    if item["authorized"] is not True:
        return "unauthorized"
    if item["fresh"] is not True:
        return "stale"
    if item["provenance_complete"] is not True:
        return "provenance-incomplete"
    if item["conflicting"] is True:
        return "conflict"
    if not item["requirements"]:
        return "no-coverage"
    return None


def _coverage_utility(
    selected: list[dict[str, Any]],
    weights: dict[str, int | float],
) -> int | float:
    covered = {
        requirement
        for item in selected
        for requirement in item["requirements"]
    }
    return sum(
        weight
        for requirement, weight in weights.items()
        if requirement in covered
    )


def select_optional_context(
    candidates: list[dict[str, Any]],
    requirement_weights: dict[str, int | float],
    *,
    budget: int,
    tokenizer: Tokenizer,
) -> dict[str, Any]:
    """Select optional context by deterministic marginal weighted coverage."""

    if budget < 0:
        raise ContextControlError("optional context budget cannot be negative")
    for requirement, weight in requirement_weights.items():
        if (
            not isinstance(requirement, str)
            or not requirement
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or weight < 0
        ):
            raise ContextControlError(
                "requirement weights must map nonempty IDs to nonnegative numbers"
            )
    prepared: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        item = _context_item(raw, inclusion_class="optional")
        artifact_id = item["artifact_id"]
        if artifact_id in seen:
            raise ContextControlError(
                f"duplicate optional artifact: {artifact_id}"
            )
        seen.add(artifact_id)
        item["token_count"] = max(1, tokenizer.count_tokens(item["content"]))
        reason = _optional_eligibility(item)
        if reason is None and not any(
            requirement_weights.get(requirement, 0) > 0
            for requirement in item["requirements"]
        ):
            reason = "no-positive-coverage"
        if reason is None:
            prepared.append(item)
        else:
            omitted.append(
                {
                    "artifact_id": artifact_id,
                    "reason": reason,
                    "token_count": item["token_count"],
                }
            )
    prepared.sort(key=lambda item: item["artifact_id"])

    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    remaining = budget
    trace: list[dict[str, Any]] = []
    while True:
        feasible: list[tuple[Fraction, int | float, int, str, dict[str, Any], list[str]]] = []
        for item in prepared:
            if item in selected or item["token_count"] > remaining:
                continue
            new_requirements = sorted(
                requirement
                for requirement in item["requirements"]
                if requirement not in covered
                and requirement_weights.get(requirement, 0) > 0
            )
            gain = sum(requirement_weights[item] for item in new_requirements)
            if gain <= 0:
                continue
            density = Fraction(gain).limit_denominator() / item["token_count"]
            feasible.append(
                (
                    density,
                    gain,
                    -item["token_count"],
                    item["artifact_id"],
                    item,
                    new_requirements,
                )
            )
        if not feasible:
            break
        feasible.sort(
            key=lambda row: (-row[0], -row[1], -row[2], row[3])
        )
        _, gain, _, _, winner, new_requirements = feasible[0]
        selected.append(winner)
        covered.update(new_requirements)
        remaining -= winner["token_count"]
        trace.append(
            {
                "artifact_id": winner["artifact_id"],
                "marginal_coverage": new_requirements,
                "marginal_utility": gain,
                "token_count": winner["token_count"],
                "remaining_budget": remaining,
            }
        )

    feasible_singletons = [
        item for item in prepared if item["token_count"] <= budget
    ]
    feasible_singletons.sort(
        key=lambda item: (
            -_coverage_utility([item], requirement_weights),
            item["token_count"],
            item["artifact_id"],
        )
    )
    singleton = feasible_singletons[0] if feasible_singletons else None
    greedy_utility = _coverage_utility(selected, requirement_weights)
    singleton_utility = (
        _coverage_utility([singleton], requirement_weights)
        if singleton is not None
        else -1
    )
    strategy = "marginal-density"
    if singleton is not None and (
        singleton_utility > greedy_utility
        or (
            singleton_utility == greedy_utility
            and singleton["token_count"]
            < sum(item["token_count"] for item in selected)
        )
    ):
        selected = [singleton]
        strategy = "best-singleton"
        covered = set(singleton["requirements"])

    selected_ids = [item["artifact_id"] for item in selected]
    for item in prepared:
        if item["artifact_id"] in selected_ids:
            continue
        reason = (
            "redundant"
            if all(
                requirement in covered
                or requirement_weights.get(requirement, 0) <= 0
                for requirement in item["requirements"]
            )
            else "budget"
            if item["token_count"] > budget - sum(
                chosen["token_count"] for chosen in selected
            )
            else "not-selected"
        )
        omitted.append(
            {
                "artifact_id": item["artifact_id"],
                "reason": reason,
                "token_count": item["token_count"],
            }
        )
    omitted.sort(key=lambda item: item["artifact_id"])
    return {
        "strategy": strategy,
        "selector_version": "weighted-coverage-v1",
        "selected": selected,
        "selected_ids": selected_ids,
        "omitted": omitted,
        "trace": trace,
        "covered_requirements": sorted(covered),
        "utility": _coverage_utility(selected, requirement_weights),
        "used_tokens": sum(item["token_count"] for item in selected),
        "budget": budget,
    }


def serialize_adapter_payload(payload: list[dict[str, str]]) -> str:
    """Serialize exactly what the provider-neutral capsule adapter receives."""

    return "\n\n".join(
        f"[{item['artifact_id']}]\n{item['content']}"
        for item in payload
    )


def serialize_adapter_input(
    tokenizer: Tokenizer,
    payload: list[dict[str, str]],
) -> str:
    """Serialize the exact model input, including adapter-owned prompt framing."""

    serialized_payload = serialize_adapter_payload(payload)
    serializer = getattr(tokenizer, "serialize_prompt", None)
    if serializer is None:
        return serialized_payload
    if not callable(serializer):
        raise ContextControlError("tokenizer serialize_prompt must be callable")
    serialized_input = serializer(serialized_payload)
    if not isinstance(serialized_input, str):
        raise ContextControlError("tokenizer serialize_prompt must return text")
    return serialized_input


def count_adapter_input(
    tokenizer: Tokenizer,
    payload: list[dict[str, str]],
) -> int:
    """Count the exact provider input produced from a bounded payload."""

    return tokenizer.count_tokens(serialize_adapter_input(tokenizer, payload))


def _payload_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "artifact_id": item["artifact_id"],
        "content": item["content"],
    }


def _manifest_item(
    item: dict[str, Any],
    classification: str,
    tokenizer: Tokenizer,
    *,
    inclusion_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": item["artifact_id"],
        "artifact_version": item["version"],
        "classification": classification,
        "inclusion_class": item["inclusion_class"],
        "inclusion_reason": inclusion_reason
        or (
            f"required-{item['inclusion_class']}"
            if classification == "mandatory"
            else "selected-weighted-coverage"
        ),
        "token_count": tokenizer.count_tokens(item["content"]),
        "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
        "source": item["source"],
        "requirements": item["requirements"],
    }


def _envelope_id_seed(
    graph: dict[str, Any],
    task_node_id: str,
    mandatory: list[dict[str, Any]],
    optional: list[dict[str, Any]],
    tokenizer: Tokenizer,
    mode: str,
    input_limit: int,
    output_reserve: int,
    safety_margin: int,
    checkpoint_id: str,
    adapter_identity: str,
    compactor_version: str,
) -> str:
    seed = {
        "graph_checksum": sha256_json(graph),
        "task_node_id": task_node_id,
        "mandatory": [
            [
                item["artifact_id"],
                item["version"],
                hashlib.sha256(item["content"].encode()).hexdigest(),
            ]
            for item in mandatory
        ],
        "optional": [
            [
                item["artifact_id"],
                item.get("version", "unversioned"),
                hashlib.sha256(item["content"].encode()).hexdigest(),
            ]
            for item in optional
        ],
        "tokenizer": [tokenizer.tokenizer_id, tokenizer.exact],
        "checkpoint_id": checkpoint_id,
        "adapter_identity": adapter_identity,
        "compactor_version": compactor_version,
        "mode": mode,
        "budget": [input_limit, output_reserve, safety_margin],
    }
    return f"envelope-{sha256_json(seed)[:20]}"


def _overflow_result(
    envelope_id: str,
    task_node_id: str,
    mandatory: list[dict[str, Any]],
    used_context: int,
    available_context: int,
) -> dict[str, Any]:
    return seal_document(
        {
            "schema_version": 1,
            "envelope_id": envelope_id,
            "task_node_id": task_node_id,
            "status": "mandatory_overflow",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "provenance": [
                {
                    "source": item["source"],
                    "sha256": hashlib.sha256(
                        item["content"].encode("utf-8")
                    ).hexdigest(),
                }
                for item in mandatory
            ],
            "error": (
                f"mandatory context requires {used_context} tokens but only "
                f"{available_context} are available"
            ),
        }
    )


def plan_context(
    graph: dict[str, Any],
    task_node_id: str,
    mandatory_items: list[dict[str, Any]],
    optional_candidates: list[dict[str, Any]],
    requirement_weights: dict[str, int | float],
    *,
    tokenizer: Tokenizer,
    mode: str,
    input_limit: int,
    output_reserve: int,
    safety_margin: int,
    request_policy: dict[str, Any] | None = None,
    checkpoint_id: str = "offline-plan",
    adapter_identity: str = "fake/deterministic-capsule/1",
    compactor_version: str = "none",
) -> dict[str, Any]:
    """Plan and seal one bounded context envelope without invoking a model."""

    if mode not in {"preview", "executable"}:
        raise ContextControlError("mode must be preview or executable")
    if mode == "executable" and tokenizer.exact is not True:
        raise ContextControlError(
            "executable context planning requires an exact tokenizer"
        )
    for label, value in (
        ("checkpoint_id", checkpoint_id),
        ("adapter_identity", adapter_identity),
        ("compactor_version", compactor_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ContextControlError(f"{label} must be a nonempty string")
    if (
        isinstance(input_limit, bool)
        or input_limit < 1
        or output_reserve < 0
        or safety_margin < 0
    ):
        raise ContextControlError("context budget values are invalid")
    mandatory = sorted(
        (_mandatory_item(dict(item)) for item in mandatory_items),
        key=lambda item: (
            MANDATORY_CLASS_ORDER[item["inclusion_class"]],
            item["artifact_id"],
        ),
    )
    mandatory_ids = [item["artifact_id"] for item in mandatory]
    if len(mandatory_ids) != len(set(mandatory_ids)):
        raise ContextControlError("mandatory artifacts must be unique")
    available_context = max(
        0,
        input_limit - output_reserve - safety_margin,
    )
    mandatory_payload = [_payload_item(item) for item in mandatory]
    mandatory_tokens = count_adapter_input(tokenizer, mandatory_payload)
    envelope_id = _envelope_id_seed(
        graph,
        task_node_id,
        mandatory,
        optional_candidates,
        tokenizer,
        mode,
        input_limit,
        output_reserve,
        safety_margin,
        checkpoint_id,
        adapter_identity,
        compactor_version,
    )
    if mandatory_tokens > available_context:
        return {
            "status": "mandatory_overflow",
            "result": _overflow_result(
                envelope_id,
                task_node_id,
                mandatory,
                mandatory_tokens,
                available_context,
            ),
            "mandatory_tokens": mandatory_tokens,
            "available_context": available_context,
        }

    selection = select_optional_context(
        optional_candidates,
        requirement_weights,
        budget=max(0, available_context - mandatory_tokens),
        tokenizer=tokenizer,
    )
    selected = list(selection["selected"])
    while True:
        payload = mandatory_payload + [_payload_item(item) for item in selected]
        used_context = count_adapter_input(tokenizer, payload)
        if used_context <= available_context or not selected:
            break
        removed = selected.pop()
        selection["omitted"].append(
            {
                "artifact_id": removed["artifact_id"],
                "reason": "serialization-overflow",
                "token_count": removed["token_count"],
            }
        )
    selection["selected"] = selected
    selection["selected_ids"] = [item["artifact_id"] for item in selected]
    selection["covered_requirements"] = sorted(
        {
            requirement
            for item in selected
            for requirement in item["requirements"]
            if requirement_weights.get(requirement, 0) > 0
        }
    )
    selection["utility"] = _coverage_utility(selected, requirement_weights)
    selection["used_tokens"] = sum(
        item["token_count"] for item in selected
    )
    selection["omitted"] = sorted(
        selection["omitted"],
        key=lambda item: item["artifact_id"],
    )
    manifest = [
        _manifest_item(item, "mandatory", tokenizer)
        for item in mandatory
    ] + [
        _manifest_item(item, "optional", tokenizer)
        for item in selected
    ]
    node_contract = next(
        (
            item
            for item in mandatory
            if item["artifact_id"] == f"node-contract:{task_node_id}"
        ),
        None,
    )
    node_contract_digest = (
        node_contract["version"]
        if node_contract is not None and SHA256.fullmatch(node_contract["version"])
        else sha256_json(
            {
                "task_node_id": task_node_id,
                "contract": node_contract,
            }
        )
    )
    category_tokens = {
        "fixed_tokens": 0,
        "capability_schema_tokens": 0,
        "mandatory_tokens": 0,
        "optional_tokens": 0,
        "dynamic_tokens": 0,
    }
    selected_ids = {
        item["artifact_id"]
        for item in selected
    }
    cumulative: list[dict[str, str]] = []
    previous_tokens = 0
    for item in mandatory + selected:
        cumulative.append(_payload_item(item))
        current_tokens = tokenizer.count_tokens(
            serialize_adapter_payload(cumulative)
        )
        delta = current_tokens - previous_tokens
        previous_tokens = current_tokens
        if item["artifact_id"] in selected_ids:
            category_tokens["optional_tokens"] += delta
        elif item["inclusion_class"] == "capability":
            category_tokens["capability_schema_tokens"] += delta
        else:
            category_tokens["mandatory_tokens"] += delta
    fixed_tokens = used_context - previous_tokens
    if fixed_tokens < 0:
        raise ContextControlError(
            "adapter prompt framing cannot reduce payload token count"
        )
    category_tokens["fixed_tokens"] = fixed_tokens
    selection_evidence = {
        "selector_version": selection["selector_version"],
        "strategy": selection["strategy"],
        "selected_ids": selection["selected_ids"],
        "omitted": selection["omitted"],
        "trace": selection["trace"],
        "covered_requirements": selection["covered_requirements"],
        "utility": selection["utility"],
        "used_tokens": selection["used_tokens"],
        "budget": selection["budget"],
    }
    policy = request_policy or {
        "allowed_kinds": [],
        "allowed_capabilities": [],
        "max_additional_tokens": 0,
    }
    policy = {
        "allowed_kinds": sorted(set(policy.get("allowed_kinds", []))),
        "allowed_capabilities": sorted(
            set(policy.get("allowed_capabilities", []))
        ),
        "max_additional_tokens": policy.get("max_additional_tokens", 0),
    }
    envelope = seal_document(
        {
            "schema_version": 1,
            "envelope_id": envelope_id,
            "mode": mode,
            "graph": {
                "graph_id": graph["graph_id"],
                "graph_version": graph["graph_version"],
                "context_version": graph["project"]["context_version"],
                "canonical_sha256": sha256_json(graph),
            },
            "task_node_id": task_node_id,
            "execution_context": {
                "checkpoint_id": checkpoint_id,
                "node_contract_digest": node_contract_digest,
                "adapter_identity": adapter_identity,
                "selector_version": selection["selector_version"],
                "compactor_version": compactor_version,
            },
            "budget": {
                "input_limit": input_limit,
                "output_reserve": output_reserve,
                "safety_margin": safety_margin,
                "available_context": available_context,
                "used_context": used_context,
                **category_tokens,
                "tokenizer": {
                    "id": tokenizer.tokenizer_id,
                    "exact": tokenizer.exact,
                },
            },
            "manifest": manifest,
            "payload": payload,
            "selection": selection_evidence,
            "request_policy": policy,
            "parent_request_digest": None,
        }
    )
    errors = validate_envelope(envelope)
    if errors:
        raise ContextControlError(
            "planned envelope failed validation: " + "; ".join(errors)
        )
    return {
        "status": "planned",
        "envelope": envelope,
        "selection": selection,
        "mandatory_tokens": mandatory_tokens,
        "available_context": available_context,
    }


def extend_envelope(
    parent: dict[str, Any],
    projection_items: list[dict[str, Any]],
    *,
    tokenizer: Tokenizer,
    parent_request_digest: str,
) -> dict[str, Any]:
    """Create a new sealed envelope from approved bounded projections."""

    parent_errors = validate_envelope(parent)
    if parent_errors:
        raise ContextControlError(
            "parent envelope is invalid: " + "; ".join(parent_errors)
        )
    if parent["mode"] == "executable" and tokenizer.exact is not True:
        raise ContextControlError("executable extension requires an exact tokenizer")
    if tokenizer.tokenizer_id != parent["budget"]["tokenizer"]["id"]:
        raise ContextControlError("extension tokenizer differs from parent envelope")
    if not SHA256.fullmatch(parent_request_digest):
        raise ContextControlError("parent_request_digest must be a SHA-256 digest")

    dynamic = []
    for raw in projection_items:
        candidate = dict(raw)
        candidate.setdefault("inclusion_class", "direct_input")
        dynamic.append(_mandatory_item(candidate))
    existing_ids = {item["artifact_id"] for item in parent["manifest"]}
    duplicates = sorted(
        item["artifact_id"]
        for item in dynamic
        if item["artifact_id"] in existing_ids
    )
    if duplicates:
        raise ContextControlError(
            "projection artifacts already exist in parent: " + ", ".join(duplicates)
        )

    mandatory_manifest: list[dict[str, Any]] = []
    optional_manifest: list[dict[str, Any]] = []
    mandatory_payload: list[dict[str, str]] = []
    optional_payload: list[dict[str, str]] = []
    for manifest_item, payload_item in zip(
        parent["manifest"],
        parent["payload"],
        strict=True,
    ):
        if manifest_item["classification"] == "mandatory":
            mandatory_manifest.append(copy.deepcopy(manifest_item))
            mandatory_payload.append(copy.deepcopy(payload_item))
        else:
            optional_manifest.append(copy.deepcopy(manifest_item))
            optional_payload.append(copy.deepcopy(payload_item))
    dynamic.sort(key=lambda item: item["artifact_id"])
    dynamic_manifest = [
        _manifest_item(
            item,
            "mandatory",
            tokenizer,
            inclusion_reason="broker-approved-request",
        )
        for item in dynamic
    ]
    dynamic_payload = [_payload_item(item) for item in dynamic]
    manifest = mandatory_manifest + dynamic_manifest + optional_manifest
    payload = mandatory_payload + dynamic_payload + optional_payload
    used_context = count_adapter_input(tokenizer, payload)
    if used_context > parent["budget"]["available_context"]:
        raise ContextControlError(
            "approved projection exceeds the remaining envelope token budget"
        )
    added_tokens = max(
        0,
        used_context - parent["budget"]["used_context"],
    )
    if added_tokens > parent["request_policy"]["max_additional_tokens"]:
        raise ContextControlError(
            "approved projection exceeds request_policy.max_additional_tokens"
        )
    identity_seed = {
        "parent_envelope_digest": parent["digest"],
        "parent_request_digest": parent_request_digest,
        "projection_digests": [
            hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            for item in dynamic
        ],
    }
    result = seal_document(
        {
            "schema_version": 1,
            "envelope_id": f"envelope-{sha256_json(identity_seed)[:20]}",
            "mode": parent["mode"],
            "graph": copy.deepcopy(parent["graph"]),
            "task_node_id": parent["task_node_id"],
            "execution_context": copy.deepcopy(parent["execution_context"]),
            "budget": {
                **copy.deepcopy(parent["budget"]),
                "used_context": used_context,
                "dynamic_tokens": (
                    parent["budget"]["dynamic_tokens"] + added_tokens
                ),
            },
            "manifest": manifest,
            "payload": payload,
            "selection": copy.deepcopy(parent["selection"]),
            "request_policy": {
                **copy.deepcopy(parent["request_policy"]),
                "max_additional_tokens": max(
                    0,
                    parent["request_policy"]["max_additional_tokens"]
                    - added_tokens,
                ),
            },
            "parent_request_digest": parent_request_digest,
        }
    )
    errors = validate_envelope(result)
    if errors:
        raise ContextControlError(
            "extended envelope failed validation: " + "; ".join(errors)
        )
    return result


def build_result_key(components: dict[str, Any]) -> str:
    """Build the exact cross-run cache key from all result-affecting fields."""

    missing = sorted(RESULT_KEY_FIELDS - set(components))
    extra = sorted(set(components) - RESULT_KEY_FIELDS)
    if missing or extra:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ContextControlError("invalid result-key components: " + " ".join(details))
    return sha256_json(
        {
            "schema_version": 1,
            **components,
        }
    )


def delta_affected_nodes(
    graph: dict[str, Any],
    changed_artifacts: set[str],
) -> list[str]:
    """Return the deterministic forward invalidation set for an artifact delta."""

    from graph_core import affected_node_ids, artifact_affected_nodes

    seeds = artifact_affected_nodes(graph, changed_artifacts)
    return affected_node_ids(graph, seeds)


def early_cutoff(previous_output_digest: str, recomputed_output_digest: str) -> bool:
    """Return true when unchanged output permits downstream cutoff."""

    if not SHA256.fullmatch(previous_output_digest) or not SHA256.fullmatch(
        recomputed_output_digest
    ):
        raise ContextControlError("early-cutoff inputs must be SHA-256 digests")
    return previous_output_digest == recomputed_output_digest


def _object(
    value: Any,
    path: str,
    *,
    required: set[str],
    allowed: set[str],
    errors: list[str],
) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return False
    for field in sorted(required - set(value)):
        errors.append(f"{path}.{field}: is required")
    for field in sorted(set(value) - allowed):
        errors.append(f"{path}.{field}: is not allowed")
    return True


def _string(value: Any, path: str, errors: list[str], *, slug: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: must be a non-empty string")
        return False
    if slug and not SLUG.fullmatch(value):
        errors.append(f"{path}: must be a lowercase slug")
        return False
    return True


def _integer(value: Any, path: str, errors: list[str], *, minimum: int = 0) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{path}: must be an integer >= {minimum}")
        return False
    return True


def _sha(value: Any, path: str, errors: list[str]) -> bool:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        errors.append(f"{path}: must be a lowercase SHA-256 digest")
        return False
    return True


def _portable_source(value: Any, path: str, errors: list[str]) -> bool:
    if not _string(value, path, errors):
        return False
    assert isinstance(value, str)
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "\\" in value:
        errors.append(f"{path}: must be a portable project-relative source or identifier")
        return False
    return True


def _string_array(
    value: Any,
    path: str,
    errors: list[str],
    *,
    minimum: int = 0,
    slug: bool = False,
) -> bool:
    if not isinstance(value, list) or len(value) < minimum:
        errors.append(f"{path}: must be an array with at least {minimum} item(s)")
        return False
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if _string(item, item_path, errors, slug=slug):
            assert isinstance(item, str)
            if item in seen:
                errors.append(f"{item_path}: duplicates {item!r}")
            seen.add(item)
    return True


def _digest_error(value: dict[str, Any], errors: list[str]) -> None:
    digest = value.get("digest")
    if not _sha(digest, "$.digest", errors):
        return
    body = {key: item for key, item in value.items() if key != "digest"}
    expected = sha256_json(body)
    if digest != expected:
        errors.append("$.digest: does not match canonical document content")


def validate_envelope(value: Any) -> list[str]:
    errors: list[str] = []
    if not _object(
        value,
        "$",
        required=ENVELOPE_FIELDS,
        allowed=ENVELOPE_FIELDS,
        errors=errors,
    ):
        return errors
    assert isinstance(value, dict)
    errors.extend(_secret_errors(value))
    if value.get("schema_version") != 1:
        errors.append("$.schema_version: must equal 1")
    _string(value.get("envelope_id"), "$.envelope_id", errors, slug=True)
    mode = value.get("mode")
    if mode not in {"preview", "executable"}:
        errors.append("$.mode: must be preview or executable")
    _string(value.get("task_node_id"), "$.task_node_id", errors, slug=True)

    execution_context = value.get("execution_context")
    execution_fields = {
        "checkpoint_id",
        "node_contract_digest",
        "adapter_identity",
        "selector_version",
        "compactor_version",
    }
    if _object(
        execution_context,
        "$.execution_context",
        required=execution_fields,
        allowed=execution_fields,
        errors=errors,
    ):
        _string(
            execution_context.get("checkpoint_id"),
            "$.execution_context.checkpoint_id",
            errors,
        )
        _sha(
            execution_context.get("node_contract_digest"),
            "$.execution_context.node_contract_digest",
            errors,
        )
        for field in (
            "adapter_identity",
            "selector_version",
            "compactor_version",
        ):
            _string(
                execution_context.get(field),
                f"$.execution_context.{field}",
                errors,
            )

    graph = value.get("graph")
    graph_fields = {
        "graph_id",
        "graph_version",
        "context_version",
        "canonical_sha256",
    }
    if _object(
        graph,
        "$.graph",
        required=graph_fields,
        allowed=graph_fields,
        errors=errors,
    ):
        _string(graph.get("graph_id"), "$.graph.graph_id", errors, slug=True)
        _integer(graph.get("graph_version"), "$.graph.graph_version", errors, minimum=1)
        _string(graph.get("context_version"), "$.graph.context_version", errors)
        _sha(graph.get("canonical_sha256"), "$.graph.canonical_sha256", errors)

    budget = value.get("budget")
    budget_fields = {
        "input_limit",
        "output_reserve",
        "safety_margin",
        "available_context",
        "used_context",
        "fixed_tokens",
        "capability_schema_tokens",
        "mandatory_tokens",
        "optional_tokens",
        "dynamic_tokens",
        "tokenizer",
    }
    if _object(
        budget,
        "$.budget",
        required=budget_fields,
        allowed=budget_fields,
        errors=errors,
    ):
        _integer(budget.get("input_limit"), "$.budget.input_limit", errors, minimum=1)
        for field in (
            "output_reserve",
            "safety_margin",
            "available_context",
            "used_context",
            "fixed_tokens",
            "capability_schema_tokens",
            "mandatory_tokens",
            "optional_tokens",
            "dynamic_tokens",
        ):
            _integer(budget.get(field), f"$.budget.{field}", errors)
        tokenizer = budget.get("tokenizer")
        if _object(
            tokenizer,
            "$.budget.tokenizer",
            required={"id", "exact"},
            allowed={"id", "exact"},
            errors=errors,
        ):
            _string(tokenizer.get("id"), "$.budget.tokenizer.id", errors)
            if not isinstance(tokenizer.get("exact"), bool):
                errors.append("$.budget.tokenizer.exact: must be a boolean")
            elif mode == "executable" and tokenizer["exact"] is not True:
                errors.append(
                    "$.budget.tokenizer.exact: executable mode requires an exact tokenizer"
                )
        numeric = all(
            isinstance(budget.get(field), int)
            and not isinstance(budget.get(field), bool)
            for field in (
                "input_limit",
                "output_reserve",
                "safety_margin",
                "available_context",
                "used_context",
                "fixed_tokens",
                "capability_schema_tokens",
                "mandatory_tokens",
                "optional_tokens",
                "dynamic_tokens",
            )
        )
        if numeric:
            expected_available = (
                budget["input_limit"]
                - budget["output_reserve"]
                - budget["safety_margin"]
            )
            if budget["available_context"] != max(0, expected_available):
                errors.append(
                    "$.budget.available_context: must equal input_limit minus "
                    "output_reserve and safety_margin, floored at zero"
                )
            if budget["used_context"] > budget["available_context"]:
                errors.append("$.budget.used_context: exceeds available_context")
            categories = (
                budget["fixed_tokens"]
                + budget["capability_schema_tokens"]
                + budget["mandatory_tokens"]
                + budget["optional_tokens"]
                + budget["dynamic_tokens"]
            )
            if categories != budget["used_context"]:
                errors.append(
                    "$.budget: token categories must sum to used_context"
                )

    manifest = value.get("manifest")
    manifest_ids: list[str] = []
    optional_seen = False
    if not isinstance(manifest, list):
        errors.append("$.manifest: must be an array")
    else:
        for index, item in enumerate(manifest):
            path = f"$.manifest[{index}]"
            fields = {
                "artifact_id",
                "artifact_version",
                "classification",
                "inclusion_class",
                "inclusion_reason",
                "token_count",
                "sha256",
                "source",
                "requirements",
            }
            if not _object(
                item,
                path,
                required=fields,
                allowed=fields,
                errors=errors,
            ):
                continue
            artifact_id = item.get("artifact_id")
            if _string(artifact_id, f"{path}.artifact_id", errors):
                assert isinstance(artifact_id, str)
                if artifact_id in manifest_ids:
                    errors.append(f"{path}.artifact_id: duplicates {artifact_id!r}")
                manifest_ids.append(artifact_id)
            classification = item.get("classification")
            if classification not in {"mandatory", "optional"}:
                errors.append(f"{path}.classification: must be mandatory or optional")
            elif classification == "optional":
                optional_seen = True
            elif optional_seen:
                errors.append(
                    "$.manifest: mandatory items must precede optional items"
                )
            _string(
                item.get("artifact_version"),
                f"{path}.artifact_version",
                errors,
            )
            inclusion_class = item.get("inclusion_class")
            if inclusion_class not in set(MANDATORY_CLASS_ORDER) | {"optional"}:
                errors.append(f"{path}.inclusion_class: is unsupported")
            if (
                classification == "optional"
                and inclusion_class != "optional"
            ):
                errors.append(
                    f"{path}.inclusion_class: optional classification requires optional"
                )
            if (
                classification == "mandatory"
                and inclusion_class == "optional"
            ):
                errors.append(
                    f"{path}.inclusion_class: mandatory classification cannot be optional"
                )
            _string(
                item.get("inclusion_reason"),
                f"{path}.inclusion_reason",
                errors,
            )
            _integer(item.get("token_count"), f"{path}.token_count", errors)
            _sha(item.get("sha256"), f"{path}.sha256", errors)
            _portable_source(item.get("source"), f"{path}.source", errors)
            _string_array(
                item.get("requirements"),
                f"{path}.requirements",
                errors,
            )

    payload = value.get("payload")
    payload_ids: list[str] = []
    if not isinstance(payload, list):
        errors.append("$.payload: must be an array")
    else:
        for index, item in enumerate(payload):
            path = f"$.payload[{index}]"
            if not _object(
                item,
                path,
                required={"artifact_id", "content"},
                allowed={"artifact_id", "content"},
                errors=errors,
            ):
                continue
            artifact_id = item.get("artifact_id")
            if _string(artifact_id, f"{path}.artifact_id", errors):
                assert isinstance(artifact_id, str)
                if artifact_id in payload_ids:
                    errors.append(f"{path}.artifact_id: duplicates {artifact_id!r}")
                payload_ids.append(artifact_id)
            content = item.get("content")
            if not isinstance(content, str):
                errors.append(f"{path}.content: must be a string")
            if (
                index < len(manifest_ids)
                and isinstance(content, str)
                and isinstance(manifest, list)
                and index < len(manifest)
                and isinstance(manifest[index], dict)
            ):
                expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if manifest[index].get("sha256") != expected:
                    errors.append(
                        f"$.manifest[{index}].sha256: does not match payload content"
                    )
        if payload_ids != manifest_ids:
            errors.append("$.payload: artifact ordering must match manifest exactly")

    selection = value.get("selection")
    selection_fields = {
        "selector_version",
        "strategy",
        "selected_ids",
        "omitted",
        "trace",
        "covered_requirements",
        "utility",
        "used_tokens",
        "budget",
    }
    if _object(
        selection,
        "$.selection",
        required=selection_fields,
        allowed=selection_fields,
        errors=errors,
    ):
        _string(
            selection.get("selector_version"),
            "$.selection.selector_version",
            errors,
        )
        _string(selection.get("strategy"), "$.selection.strategy", errors)
        _string_array(
            selection.get("selected_ids"),
            "$.selection.selected_ids",
            errors,
        )
        _string_array(
            selection.get("covered_requirements"),
            "$.selection.covered_requirements",
            errors,
        )
        _integer(selection.get("used_tokens"), "$.selection.used_tokens", errors)
        _integer(selection.get("budget"), "$.selection.budget", errors)
        utility = selection.get("utility")
        if (
            isinstance(utility, bool)
            or not isinstance(utility, (int, float))
            or utility < 0
        ):
            errors.append("$.selection.utility: must be a nonnegative number")
        omitted = selection.get("omitted")
        if not isinstance(omitted, list):
            errors.append("$.selection.omitted: must be an array")
        else:
            for index, item in enumerate(omitted):
                path = f"$.selection.omitted[{index}]"
                if _object(
                    item,
                    path,
                    required={"artifact_id", "reason", "token_count"},
                    allowed={"artifact_id", "reason", "token_count"},
                    errors=errors,
                ):
                    _string(item.get("artifact_id"), f"{path}.artifact_id", errors)
                    _string(item.get("reason"), f"{path}.reason", errors)
                    _integer(item.get("token_count"), f"{path}.token_count", errors)
        trace = selection.get("trace")
        if not isinstance(trace, list):
            errors.append("$.selection.trace: must be an array")
        if isinstance(execution_context, dict) and (
            selection.get("selector_version")
            != execution_context.get("selector_version")
        ):
            errors.append(
                "$.selection.selector_version: must match execution_context"
            )
        optional_ids = [
            item.get("artifact_id")
            for item in manifest
            if isinstance(item, dict)
            and item.get("classification") == "optional"
        ] if isinstance(manifest, list) else []
        if selection.get("selected_ids") != optional_ids:
            errors.append(
                "$.selection.selected_ids: must match optional manifest order"
            )

    policy = value.get("request_policy")
    policy_fields = {
        "allowed_kinds",
        "allowed_capabilities",
        "max_additional_tokens",
    }
    if _object(
        policy,
        "$.request_policy",
        required=policy_fields,
        allowed=policy_fields,
        errors=errors,
    ):
        kinds = policy.get("allowed_kinds")
        if _string_array(kinds, "$.request_policy.allowed_kinds", errors):
            for index, kind in enumerate(kinds):
                if kind not in {"context", "artifact", "action"}:
                    errors.append(
                        f"$.request_policy.allowed_kinds[{index}]: unsupported kind"
                    )
        _string_array(
            policy.get("allowed_capabilities"),
            "$.request_policy.allowed_capabilities",
            errors,
            slug=True,
        )
        _integer(
            policy.get("max_additional_tokens"),
            "$.request_policy.max_additional_tokens",
            errors,
        )
    parent_request_digest = value.get("parent_request_digest")
    if parent_request_digest is not None:
        _sha(parent_request_digest, "$.parent_request_digest", errors)
    _digest_error(value, errors)
    return errors


def validate_request(value: Any) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version",
        "request_id",
        "envelope_id",
        "task_node_id",
        "kind",
        "purpose",
        "max_tokens",
        "digest",
    }
    if not _object(
        value,
        "$",
        required=required,
        allowed=REQUEST_FIELDS,
        errors=errors,
    ):
        return errors
    assert isinstance(value, dict)
    errors.extend(_secret_errors(value))
    if value.get("schema_version") != 1:
        errors.append("$.schema_version: must equal 1")
    for field in ("request_id", "envelope_id", "task_node_id"):
        _string(value.get(field), f"$.{field}", errors, slug=True)
    kind = value.get("kind")
    if kind not in {"context", "artifact", "action"}:
        errors.append("$.kind: must be context, artifact, or action")
    _string(value.get("purpose"), "$.purpose", errors)
    _integer(value.get("max_tokens"), "$.max_tokens", errors)
    if kind in {"context", "artifact"}:
        _string_array(
            value.get("requested_artifacts"),
            "$.requested_artifacts",
            errors,
            minimum=1,
        )
        if "capability" in value:
            errors.append("$.capability: is not allowed for a context or artifact request")
        if "arguments" in value:
            errors.append("$.arguments: is not allowed for a context or artifact request")
    elif kind == "action":
        _string(value.get("capability"), "$.capability", errors, slug=True)
        if not isinstance(value.get("arguments"), dict):
            errors.append("$.arguments: must be an object")
        if "requested_artifacts" in value:
            errors.append("$.requested_artifacts: is not allowed for an action request")
    _digest_error(value, errors)
    return errors


def validate_result(value: Any) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version",
        "envelope_id",
        "task_node_id",
        "status",
        "usage",
        "provenance",
        "digest",
    }
    if not _object(
        value,
        "$",
        required=required,
        allowed=RESULT_FIELDS,
        errors=errors,
    ):
        return errors
    assert isinstance(value, dict)
    errors.extend(_secret_errors(value))
    if value.get("schema_version") != 1:
        errors.append("$.schema_version: must equal 1")
    for field in ("envelope_id", "task_node_id"):
        _string(value.get(field), f"$.{field}", errors, slug=True)
    status = value.get("status")
    if status not in RESULT_STATUSES:
        errors.append("$.status: is unsupported")
    if status == "completed":
        if "output_artifacts" not in value:
            errors.append("$.output_artifacts: is required for completed")
        else:
            _string_array(value["output_artifacts"], "$.output_artifacts", errors)
        if "result_payload" not in value:
            errors.append("$.result_payload: is required for completed")
        for field in ("request", "error"):
            if field in value:
                errors.append(f"$.{field}: is not allowed for completed")
    elif status in {"context_request", "action_request"}:
        nested = value.get("request")
        nested_errors = validate_request(nested)
        errors.extend(
            "$.request" + error[1:] if error.startswith("$") else error
            for error in nested_errors
        )
        if isinstance(nested, dict):
            expected_kind = (
                {"context", "artifact"}
                if status == "context_request"
                else {"action"}
            )
            if nested.get("kind") not in expected_kind:
                errors.append(
                    "$.request.kind: does not match the result request status"
                )
            for field in ("envelope_id", "task_node_id"):
                if nested.get(field) != value.get(field):
                    errors.append(
                        f"$.request.{field}: must match the parent result"
                    )
        for field in ("output_artifacts", "result_payload", "error"):
            if field in value:
                errors.append(f"$.{field}: is not allowed for a request result")
    elif status in RESULT_STATUSES:
        _string(value.get("error"), "$.error", errors)
        for field in ("output_artifacts", "result_payload", "request"):
            if field in value:
                errors.append(f"$.{field}: is not allowed for {status}")

    usage = value.get("usage")
    usage_fields = {"input_tokens", "output_tokens", "total_tokens"}
    if _object(
        usage,
        "$.usage",
        required=usage_fields,
        allowed=usage_fields,
        errors=errors,
    ):
        for field in usage_fields:
            _integer(usage.get(field), f"$.usage.{field}", errors)
        if all(
            isinstance(usage.get(field), int)
            and not isinstance(usage.get(field), bool)
            for field in usage_fields
        ) and usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
            errors.append(
                "$.usage.total_tokens: must equal input_tokens plus output_tokens"
            )

    provenance = value.get("provenance")
    if not isinstance(provenance, list):
        errors.append("$.provenance: must be an array")
    else:
        for index, item in enumerate(provenance):
            path = f"$.provenance[{index}]"
            if _object(
                item,
                path,
                required={"source", "sha256"},
                allowed={"source", "sha256"},
                errors=errors,
            ):
                _portable_source(item.get("source"), f"{path}.source", errors)
                _sha(item.get("sha256"), f"{path}.sha256", errors)
    _digest_error(value, errors)
    return errors


def _assert_valid(errors: list[str]) -> None:
    if errors:
        raise ContextValidationError(errors)


def assert_valid_envelope(value: Any) -> None:
    _assert_valid(validate_envelope(value))


def assert_valid_request(value: Any) -> None:
    _assert_valid(validate_request(value))


def assert_valid_result(value: Any) -> None:
    _assert_valid(validate_result(value))
