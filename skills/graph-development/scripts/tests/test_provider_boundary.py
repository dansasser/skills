from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
SKILL_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from distribution_manifest import build_distribution_manifest  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402
from context_core import DeterministicWordTokenizer, plan_context  # noqa: E402


FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "anthropic",
        "httpx",
        "ollama",
        "openai",
        "requests",
        "socket",
        "tiktoken",
        "urllib",
    }
)
CORE_ENTRYPOINTS = (
    "add_specification_decisions.py",
    "analyze_token_efficiency.py",
    "contextctl.py",
    "distribution_manifest.py",
    "graph_worker.py",
    "graphctl.py",
    "new_graph.py",
    "render_graph.py",
    "rewrite_graph.py",
    "specctl.py",
    "validate_graph.py",
    "validate_specifications.py",
)
PROVIDER_EXECUTABLE_NAMES = frozenset(
    {
        "ablation_runner.py",
        "fetch_kimi_tokenizer.py",
        "ollama_conformance.py",
        "prepare_blind_review.py",
        "retained_corpus.py",
        "run_blind_model_review.py",
        "summarize_results.py",
    }
)


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


class ProviderBoundaryTests(unittest.TestCase):
    def test_core_runtime_has_no_provider_or_network_imports(self) -> None:
        violations: list[str] = []
        for path in sorted(SCRIPTS.glob("*.py")):
            forbidden = sorted(_import_roots(path) & FORBIDDEN_IMPORT_ROOTS)
            if forbidden:
                violations.append(f"{path.name}: {', '.join(forbidden)}")
        self.assertEqual(violations, [])

    def test_core_entrypoints_import_with_site_disabled_from_unrelated_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unrelated = Path(directory) / "unrelated cwd"
            unrelated.mkdir()
            environment = {
                "PATH": os.environ.get("PATH", os.defpath),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            failures: list[str] = []
            for name in CORE_ENTRYPOINTS:
                completed = subprocess.run(
                    [sys.executable, "-S", str(SCRIPTS / name), "--help"],
                    cwd=unrelated,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode != 0:
                    diagnostic = (completed.stderr or completed.stdout).strip()
                    failures.append(f"{name}: {diagnostic[-500:]}")
            self.assertEqual(failures, [])

    def test_core_context_planning_works_when_socket_creation_is_denied(self) -> None:
        graph = valid_graph()
        tokenizer = DeterministicWordTokenizer("provider-absent-word-v1")
        mandatory = [
            {
                "artifact_id": "artifact:required",
                "content": "provider-independent context",
                "source": "docs/required.md",
                "requirements": ["acceptance"],
                "inclusion_class": "direct_input",
            }
        ]
        with mock.patch.object(
            socket,
            "socket",
            side_effect=AssertionError("network access is forbidden"),
        ):
            result = plan_context(
                graph,
                "implement-change",
                mandatory,
                [],
                {},
                tokenizer=tokenizer,
                mode="preview",
                input_limit=100,
                output_reserve=10,
                safety_margin=5,
            )
        self.assertEqual(result["status"], "planned")

    def test_clean_distribution_contains_no_provider_executable_or_requirement(self) -> None:
        manifest = build_distribution_manifest(SKILL_ROOT)
        paths = [str(entry["path"]) for entry in manifest["files"]]
        violations = [
            path
            for path in paths
            if (
                Path(path).name in PROVIDER_EXECUTABLE_NAMES
                or Path(path).name.startswith("requirements-")
                or path == "scripts/context_adapters.py"
            )
        ]
        self.assertEqual(violations, [])

    def test_ordinary_core_workflow_never_loads_provider_tooling(self) -> None:
        code = """
import json
import pathlib
import sys
scripts = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(scripts))
sys.path.insert(0, str(scripts / "tests"))
before = set(sys.modules)
from context_core import DeterministicWordTokenizer, plan_context
from tests.fixtures import valid_graph
graph = valid_graph()
result = plan_context(
    graph,
    "implement-change",
    [{
        "artifact_id": "artifact:required",
        "content": "bounded",
        "source": "docs/required.md",
        "requirements": ["acceptance"],
        "inclusion_class": "direct_input",
    }],
    [],
    {},
    tokenizer=DeterministicWordTokenizer("word-v1"),
    mode="preview",
    input_limit=50,
    output_reserve=5,
    safety_margin=2,
)
loaded = sorted(set(sys.modules) - before)
print(json.dumps({"status": result["status"], "loaded": loaded}))
"""
        completed = subprocess.run(
            [sys.executable, "-S", "-c", code, str(SCRIPTS)],
            cwd=SKILL_ROOT.parent,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "planned")
        provider_markers = ("ollama", "kimi", "tiktoken", "urllib", "socket")
        self.assertEqual(
            [
                name
                for name in report["loaded"]
                if any(marker in name.lower() for marker in provider_markers)
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
