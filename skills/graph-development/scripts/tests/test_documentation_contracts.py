from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def public_operator_docs() -> str:
    paths = [ROOT / "README.md", ROOT / "SKILL.md", *sorted((ROOT / "doc").glob("*.md"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


class DocumentationContractTests(unittest.TestCase):
    def test_public_applicability_requires_invocation_or_project_opt_in(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("explicit `$graph-development` invocation", skill)
        self.assertIn("operator-neutral project policy", skill)
        self.assertIn("Without explicit invocation or project opt-in", skill)
        self.assertIn("this public skill does not assume standing authority", skill)

    def test_v2_fast_path_profiles_and_commands_are_documented(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        specification = (ROOT / "references/specification-development.md").read_text(
            encoding="utf-8"
        )
        readme = public_operator_docs()

        self.assertIn("Fast path precedence", skill)
        self.assertIn("starter", specification)
        self.assertIn("standard", specification)
        self.assertIn("assurance", specification)
        self.assertIn(
            "Budget pressure never marks a goal blocked",
            specification,
        )
        self.assertIn("specctl.py snapshot", readme)
        self.assertIn("specctl.py sync", readme)
        self.assertIn("specctl.py verify", readme)
        self.assertLess(len(skill.splitlines()), 500)

        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", skill):
            if target.startswith(("http://", "https://", "#")):
                continue
            resolved = (ROOT / target.split("#", 1)[0]).resolve()
            self.assertTrue(resolved.is_file(), target)

    def test_resource_contract_separates_shared_reads_from_conflict_domains(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        contract = (ROOT / "references/graph-contract.md").read_text(encoding="utf-8")
        templates = (ROOT / "references/template-catalog.md").read_text(encoding="utf-8")
        self.assertIn("Shared read-only dependencies belong in `permissions.read`", skill)
        self.assertIn("Shared read-only dependencies belong in `permissions.read`, not `resources`", contract)
        self.assertIn("Ordinary shared read-only sources belong in `permissions.read`", templates)

    def test_approval_artifact_lineage_is_documented_end_to_end(self) -> None:
        rules = (ROOT / "references/validation-rules.md").read_text(encoding="utf-8")
        examples = (ROOT / "references/examples.md").read_text(encoding="utf-8")
        self.assertIn(
            "An `approves` edge lists an approval artifact produced by its human-gate source",
            rules,
        )
        self.assertIn(
            '"outputs": [\n'
            '      {"name": "migration approval", "artifact": "artifact:migration-approval", "format": "approval-record"}',
            examples,
        )
        self.assertIn(
            '"inputs": [\n'
            '      {"name": "migration approval", "artifact": "artifact:migration-approval", "required": true}',
            examples,
        )

    def test_checksum_marker_and_json_fields_are_documented(self) -> None:
        rules = (ROOT / "references/validation-rules.md").read_text(encoding="utf-8")
        readme = public_operator_docs()
        for text in (rules, readme):
            self.assertIn("<!-- development-graph-sha256: 64_lowercase_hex_characters -->", text)
            self.assertIn("canonical_sha256", text)
            self.assertIn("markdown_marker", text)

    def test_specification_development_is_a_directly_routed_workflow(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        specification = (ROOT / "references/specification-development.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "[specification development](references/specification-development.md)",
            skill,
        )
        for entry_mode in [
            "product conversation",
            "minimum specifications",
            "strong prompt",
            "existing project at any stage",
        ]:
            self.assertIn(entry_mode, specification)

    def test_optional_graph_intake_is_bounded_and_graph_owned(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        specification = (ROOT / "references/specification-development.md").read_text(
            encoding="utf-8"
        )
        combined = f"{skill}\n{specification}"

        for required in [
            "$graph-intake",
            "strong prompt or adequate minimum specifications",
            "user-confirmed",
            "conversation input only",
            "Graph Development remains the sole owner",
            "When the companion is unavailable",
            "do not install it",
            "exactly one truly blocking",
            "recommended answer",
            "built-in fallback",
            "do not claim Graph Intake ran",
            "Generic plan or design critique",
            "separate from PLG and DLG",
        ]:
            self.assertIn(required, combined)

        self.assertNotIn("graph-intake/", specification)

    def test_specification_trigger_and_near_miss_boundaries_are_explicit(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        openai_yaml = (ROOT / "agents/openai.yaml").read_text(encoding="utf-8")
        combined = f"{skill}\n{openai_yaml}".lower()
        for trigger in [
            "specification-to-delivery",
            "specification lineage",
            "unresolved decisions",
        ]:
            self.assertIn(trigger, combined)
        for near_miss in [
            "plain prd",
            "architecture-only diagram",
            "small understood change",
        ]:
            self.assertIn(near_miss, combined)

    def test_specification_operator_contract_lists_all_artifacts_and_validators(self) -> None:
        readme = public_operator_docs()
        templates = (ROOT / "references/template-catalog.md").read_text(encoding="utf-8")
        rules = (ROOT / "references/validation-rules.md").read_text(encoding="utf-8")
        for required in [
            "specification-manifest.json",
            "references/specification-manifest.schema.json",
            "scripts/validate_specifications.py",
            "scripts/add_specification_decisions.py",
            "--template specification-to-delivery",
            "Specification validation: PASS",
        ]:
            self.assertIn(required, readme)
        self.assertIn("## Template: specification-to-delivery", templates)
        self.assertIn("Decision-to-implementation lineage", rules)

    def test_specification_fast_path_and_handoff_are_explicit(self) -> None:
        specification = (ROOT / "references/specification-development.md").read_text(
            encoding="utf-8"
        )
        for required in [
            "minimum viable artifact set before elaboration",
            "Do not read entire generated graphs, schemas, or validator source",
            "list every named decision ID separately",
            "Protected files were preserved.",
            "reserve time for both validators and the final handoff",
            "PYTHONDONTWRITEBYTECODE=1",
            "protected source snapshot",
        ]:
            self.assertIn(required, specification)

    def test_specification_failure_and_status_contracts_are_explicit(self) -> None:
        specification = (ROOT / "references/specification-development.md").read_text(
            encoding="utf-8"
        )
        rules = (ROOT / "references/validation-rules.md").read_text(encoding="utf-8")
        readme = public_operator_docs()
        for required in [
            "Both `open` and `deferred` remain unresolved",
            "definition-only elaboration",
            "durable project lifecycle",
            "nonterminal transaction blocks another apply",
            "exact-current recovery",
            "resolved-path containment",
        ]:
            self.assertIn(required, f"{specification}\n{rules}\n{readme}")

    def test_cataloged_decisions_have_records_and_end_to_end_lineage(self) -> None:
        specification = (ROOT / "references/specification-development.md").read_text(
            encoding="utf-8"
        )
        templates = (ROOT / "references/template-catalog.md").read_text(
            encoding="utf-8"
        )
        for required in [
            "One durable record per material cataloged decision",
            "consume every required document and every resolved decision",
            "consume every required specification and resolved decision",
            "blocks only its declared affected nodes",
        ]:
            self.assertIn(required, specification)
        self.assertIn(
            "Every material cataloged decision has a durable record",
            templates,
        )

    def test_manifest_schema_rejects_unsafe_or_blank_paths(self) -> None:
        schema = json.loads(
            (ROOT / "references/specification-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        relative_path = re.compile(
            schema["$defs"]["relativePath"].get("pattern", ".*")
        )
        nonempty = re.compile(schema["$defs"]["nonempty"].get("pattern", ".*"))

        absolute_path = "/" + "absolute.md"
        for invalid in [absolute_path, "../outside.md", "doc/../outside.md", "   "]:
            self.assertIsNone(relative_path.fullmatch(invalid), invalid)
        self.assertIsNotNone(relative_path.fullmatch("doc/spec.md"))
        self.assertIsNone(nonempty.search(" \n\t"))
        self.assertIsNotNone(nonempty.search("artifact:value"))

    def test_v4_release_and_v3_context_control_are_documented(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = public_operator_docs()
        context = (ROOT / "references/context-control.md").read_text(
            encoding="utf-8"
        )
        graph = (ROOT / "references/graph-contract.md").read_text(encoding="utf-8")
        runtime = (ROOT / "references/runtime-contract.md").read_text(
            encoding="utf-8"
        )
        rules = (ROOT / "references/validation-rules.md").read_text(
            encoding="utf-8"
        )
        combined = "\n".join((skill, readme, context, graph, runtime, rules))

        self.assertIn("V4", readme)
        self.assertIn("provider-neutral V4 release gate", readme)
        self.assertNotIn("evaluation-summary.json", readme)
        for required in [
            "scripts/verify_distribution.py",
            "scripts/projectctl.py",
            "scripts/contextctl.py",
            "references/schema-migrations.md",
            "references/context-observability.md",
        ]:
            self.assertIn(required, combined)
        for required in [
            "context_control",
            "digest-only",
            "mandatory_overflow",
            "exact tokenizer",
            "fake adapter",
            "D5",
            "generic filesystem",
            "same-run replay",
            "clean-recomputation",
            "outer Codex",
            "checkpoint",
            "artifact version",
            "token categories",
            "request bounds",
        ]:
            self.assertIn(required.lower(), combined.lower())


if __name__ == "__main__":
    unittest.main()
