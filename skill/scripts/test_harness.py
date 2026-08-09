#!/usr/bin/env python3
"""Regression tests for the provider-neutral verified agent harness."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"


def load_harness() -> dict[str, object]:
    namespace: dict[str, object] = {
        "__name__": "verified_agent_harness_under_test",
        "__file__": str(SCRIPTS / "harness"),
    }
    for name in ("harness_core.py", "harness_commands.py"):
        path = SCRIPTS / name
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace, namespace)
    return namespace


H = load_harness()
HarnessError = H["HarnessError"]


class ContractTests(unittest.TestCase):
    def test_agent_adapter_contract_is_complete_and_fail_closed(self) -> None:
        cfg = {
            "agent_runtime": {
                "adapter_argv": ["/opt/harness/adapters/reference"],
                "ephemeral": True,
                "models": {"implementer": "writer-alias"},
            }
        }
        argv = H["agent_adapter_argv"](
            cfg,
            Path("/work/project"),
            "implementer",
            Path("/work/prompt.md"),
            Path("/contracts/implementation.json"),
            Path("/work/output.json"),
        )
        self.assertEqual(argv[:1], ["/opt/harness/adapters/reference"])
        values = dict(zip(argv[1::2], argv[2::2]))
        self.assertEqual(values, {
            "--role": "Implementer",
            "--access": "workspace-write",
            "--workdir": "/work/project",
            "--prompt": "/work/prompt.md",
            "--schema": "/contracts/implementation.json",
            "--output": "/work/output.json",
            "--model-alias": "writer-alias",
            "--ephemeral": "true",
        })
        reviewer = H["agent_adapter_argv"](
            cfg, Path("/work/project"), "reviewer", Path("/work/prompt.md"),
            Path("/contracts/review.json"), Path("/work/output.json"),
        )
        self.assertIn("read-only", reviewer)
        cfg["agent_runtime"]["ephemeral"] = False
        with self.assertRaisesRegex(HarnessError, "ephemeral"):
            H["agent_adapter_argv"](
                cfg, Path("/work/project"), "implementer", Path("/work/prompt.md"),
                Path("/contracts/implementation.json"), Path("/work/output.json"),
            )
        reference_adapter = (SKILL_ROOT / "adapters/codex_cli.py").read_text(encoding="utf-8")
        self.assertNotIn("HARNESS_CODEX_BIN", reference_adapter)

    def test_core_branding_and_role_vocabulary_are_provider_neutral(self) -> None:
        self.assertIn("name: verified-agent-harness", (SKILL_ROOT / "SKILL.md").read_text())
        for relative in (
            "scripts/harness", "scripts/harness_core.py", "scripts/harness_commands.py",
            "templates/implementation-prompt.md", "templates/review-prompt.md",
            "templates/test-prompt.md", "templates/security-review-prompt.md",
            "templates/verification-prompt.md", "templates/project-config.toml",
            "references/architecture.md", "references/recovery.md",
            "references/state-machine.md", "references/workflow.md",
        ):
            text = (SKILL_ROOT / relative).read_text(encoding="utf-8").lower()
            provider_lines = [line for line in text.splitlines() if "codex" in line]
            if relative in {"scripts/harness_core.py", "templates/project-config.toml"}:
                self.assertTrue(all(
                    "legacy" in line or 'if "codex" in cfg' in line
                    or "adapters/codex_cli.py" in line
                    for line in provider_lines
                ), provider_lines)
            else:
                self.assertFalse(provider_lines, relative)
            self.assertNotIn("hermes", text, relative)
        prompt_text = "\n".join(
            path.read_text(encoding="utf-8") for path in (SKILL_ROOT / "templates").glob("*-prompt.md")
        )
        for role in (
            "Implementer", "Correctness Reviewer", "Tester", "Security Reviewer", "Verifier",
        ):
            self.assertIn(role, prompt_text)
    def test_stage_parser_excludes_lifecycle_command_surface(self) -> None:
        commands = H["parser"]()._subparsers._group_actions[0].choices
        for command in ("detect", "assess", "bootstrap", "adopt", "activate"):
            self.assertNotIn(command, commands)
        for implementation in ("assess_project", "bootstrap_project", "adopt_project",
                               "activate_project"):
            self.assertNotIn(implementation, H)
        self.assertNotIn("detect_lifecycle", H)
        router_path = SKILL_ROOT.parent / "bin/harness"
        if router_path.is_file():
            # The top-level router is part of the source repository, not the installed Skill payload.
            router = router_path.read_text(encoding="utf-8")
            self.assertIn("detect|assess|bootstrap|adopt|activate|rollback-gc", router)
            self.assertIn('SCRIPT="$LIFECYCLE_SCRIPT"', router)

    def test_versions_are_synchronized(self) -> None:
        self.assertEqual(H["HARNESS_VERSION"], "1.0.0")
        self.assertIn('version: 1.0.0', (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn('harness_version = "1.0.0"',
                      (SKILL_ROOT / "templates/project-config.toml").read_text(encoding="utf-8"))

    def test_control_documents_are_protected_by_default(self) -> None:
        template = (SKILL_ROOT / "templates/project-config.toml").read_text(encoding="utf-8")
        self.assertIn('".harness/PROJECT_STATE.md"', template)
        self.assertIn('".harness/CURRENT_STAGE.md"', template)

    def test_agent_contracts_use_strict_supported_subset(self) -> None:
        forbidden = {"allOf", "anyOf", "oneOf", "if", "then", "else", "$ref"}
        for path in sorted((SKILL_ROOT / "references/contracts").glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            stack = [schema]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    self.assertTrue(forbidden.isdisjoint(value), path.name)
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
            self.assertEqual(schema.get("type"), "object")
            self.assertFalse(schema.get("additionalProperties", True))

    def test_cross_field_semantics_are_fail_closed(self) -> None:
        implementation_schema = SKILL_ROOT / "references/contracts/implementation.schema.json"
        review_schema = SKILL_ROOT / "references/contracts/review.schema.json"
        cases = [
            (implementation_schema, {
                "status": "completed", "summary": "done", "changed_files": [],
                "tests": [], "blockers": ["not actually done"],
            }),
            (implementation_schema, {
                "status": "blocked", "summary": "blocked", "changed_files": [],
                "tests": [], "blockers": [],
            }),
            (review_schema, {
                "verdict": "approved", "summary": "approved",
                "findings": [{"severity": "high", "file": "x", "line": 1,
                              "message": "finding"}], "checks_reviewed": ["diff"],
            }),
            (review_schema, {
                "verdict": "changes_required", "summary": "changes",
                "findings": [], "checks_reviewed": ["diff"],
            }),
        ]
        with tempfile.TemporaryDirectory() as raw:
            report = Path(raw) / "report.json"
            for schema, value in cases:
                report.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(HarnessError):
                    H["validate_report"](report, schema)

    def test_obsolete_duplicate_contracts_are_absent(self) -> None:
        self.assertFalse((SKILL_ROOT / "schemas/implementation.schema.json").exists())
        self.assertFalse((SKILL_ROOT / "schemas/review.schema.json").exists())
        self.assertTrue((SKILL_ROOT / "schemas/quality-gates.schema.json").is_file())

    def test_worker_templates_preserve_control_and_evidence_boundaries(self) -> None:
        implementation = (SKILL_ROOT / "templates/implementation-prompt.md").read_text(
            encoding="utf-8"
        )
        review = (SKILL_ROOT / "templates/review-prompt.md").read_text(encoding="utf-8")
        for value in (implementation, review):
            for placeholder in ("{{PROJECT_ROOT}}", "{{STAGE_ID}}", "{{SLICE_ID}}",
                                "{{ATTEMPT}}", "{{CURRENT_STAGE}}"):
                self.assertIn(placeholder, value)
        self.assertIn("Never invoke `harness run-gates`", implementation)
        self.assertIn("never fabricate missing evidence", implementation)
        self.assertIn("Do not accept Plan prose alone", review)

    def test_skill_forbids_overlapping_heavy_verification_with_active_worker(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Never overlap a live Worker or Reviewer", skill)
        self.assertIn("CARGO_BUILD_JOBS", skill)


class EnvironmentTests(unittest.TestCase):
    def test_gate_environment_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ,
            {
                "HOME": "/secret-home", "PATH": "/bin", "HTTP_PROXY": "http://secret",
                "CONDA_PREFIX": "/secret-conda", "API_KEY": "secret", "TMPDIR": "/safe-tmp",
            },
            clear=True,
        ):
            home = Path(raw)
            env = H["gate_environment"](home)
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["TMPDIR"], "/safe-tmp")
        self.assertEqual(env["CONDA_NO_PLUGINS"], "true")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["CARGO_BUILD_JOBS"], "4")
        for key in ("HTTP_PROXY", "CONDA_PREFIX", "API_KEY"):
            self.assertNotIn(key, env)

    def test_worker_environment_disables_conda_plugins_without_leaking_context(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/trusted-home", "PATH": "/bin", "LANG": "C.UTF-8",
                "CONDA_PREFIX": "/secret-conda", "API_KEY": "secret",
            },
            clear=True,
        ):
            env = H["worker_environment"]()
        self.assertEqual(env["HOME"], "/trusted-home")
        self.assertEqual(env["CONDA_NO_PLUGINS"], "true")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["CARGO_BUILD_JOBS"], "4")
        self.assertNotIn("CONDA_PREFIX", env)
        self.assertNotIn("API_KEY", env)

    def test_doctor_without_project_config_checks_portable_core_only(self) -> None:
        emitted: list[tuple[str, str, dict[str, object]]] = []
        replacements = {
            "git_root": lambda _required: None,
            "executable_version": lambda executable, _args: (True, f"{executable} available"),
            "emit": lambda command, status, **values: emitted.append((command, status, values)),
        }
        originals = {name: H[name] for name in replacements}
        H.update(replacements)
        try:
            H["doctor"](argparse.Namespace(capabilities=False))
        finally:
            H.update(originals)

        self.assertEqual(emitted[-1][1], "ok")
        self.assertEqual(set(emitted[-1][2]["checks"]), {"git", "python"})

    def test_doctor_required_adapter_failure_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            project_paths = H["paths"](root)
            config = project_paths["config"]
            config.parent.mkdir(parents=True)
            config.write_text(
                (SKILL_ROOT / "templates/project-config.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            emitted: list[tuple[str, str, dict[str, object]]] = []
            replacements = {
                "git_root": lambda _required: root,
                "paths": lambda _root: project_paths,
                "executable_version": lambda executable, _args: (True, f"{executable} available"),
                "run_capture": lambda *_args, **_kwargs: types.SimpleNamespace(
                    stdout="", returncode=1
                ),
                "emit": lambda command, status, **values: emitted.append((command, status, values)),
            }
            originals = {name: H[name] for name in replacements}
            H.update(replacements)
            try:
                with self.assertRaises(HarnessError):
                    H["doctor"](argparse.Namespace(capabilities=False))
            finally:
                H.update(originals)
        self.assertEqual(emitted[-1][1], "degraded")
        self.assertFalse(emitted[-1][2]["checks"]["agent_adapter"])

    def test_gate_executable_is_resolved_and_reportable(self) -> None:
        with mock.patch.object(H["shutil"], "which", return_value="/usr/bin/true"):
            self.assertEqual(H["executable_gate_command"](Path("/tmp"), ["tool", "--check"]),
                             [str(Path("/usr/bin/true").absolute()), "--check"])

    def test_gate_executable_preserves_path_proxy_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dispatcher = root / "dispatcher"
            proxy = root / "cargo"
            dispatcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            dispatcher.chmod(0o755)
            proxy.symlink_to(dispatcher)
            with mock.patch.object(H["shutil"], "which", return_value=str(proxy)):
                resolved = H["executable_gate_command"](root, ["cargo", "test"])
            self.assertEqual(resolved, [str(proxy.absolute()), "test"])

    def test_relative_gate_executable_resolves_from_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "scripts/verify"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            subdir = root / "nested"
            subdir.mkdir()
            with contextlib.chdir(subdir):
                resolved = H["executable_gate_command"](root, ["./scripts/verify", "full"])
            self.assertEqual(resolved, [str(executable.resolve()), "full"])

    def test_control_artifact_reader_rejects_links(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.md"
            target.write_text("safe\n", encoding="utf-8")
            symlink = root / "symlink.md"
            symlink.symlink_to(target)
            hardlink = root / "hardlink.md"
            os.link(target, hardlink)
            with self.assertRaises(HarnessError):
                H["read_single_link_text"](symlink, "symlink")
            with self.assertRaises(HarnessError):
                H["read_single_link_text"](hardlink, "hardlink")


class StateAndSnapshotTests(unittest.TestCase):
    def make_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
        (root / "protected.txt").write_text("protected\n", encoding="utf-8")
        (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "add", "protected.txt", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
        return temp, root

    def make_harness_repo(self, adapter_argv: str | None = None) -> tuple[
        tempfile.TemporaryDirectory[str], Path, dict[str, Path]
    ]:
        temp, root = self.make_repo()
        launcher = str(SCRIPTS / "harness")
        subprocess.run([launcher, "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
        if adapter_argv is not None:
            config = root / ".harness/config.toml"
            text = config.read_text(encoding="utf-8")
            text = re.sub(
                r'^adapter_argv = .*$', f'adapter_argv = {adapter_argv}',
                text, count=1, flags=re.MULTILINE,
            )
            config.write_text(text, encoding="utf-8")
        plan = root / "plan.md"
        plan.write_text(
            "# Approved test plan\n\nVerify deterministic recovery behavior and state transitions.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", ".gitignore", ".harness", "plan.md"],
                       cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initialize harness"],
                       cwd=root, check=True)
        subprocess.run(
            [launcher, "start-stage", "--stage", "S1", "--title", "Test stage",
             "--slice", "S1.1", "--plan-file", "plan.md"],
            cwd=root, check=True, stdout=subprocess.DEVNULL,
        )
        return temp, root, H["paths"](root)

    def finding(self, finding_id: str = "R-001", severity: str = "high",
                blocking: bool = True) -> dict[str, object]:
        return {"id": finding_id, "severity": severity, "category": "correctness",
                "location": "tracked.txt:1", "trigger": "synthetic trigger",
                "evidence": "synthetic evidence", "reproduction": ["inspect tracked.txt"],
                "confidence": "high", "blocking_recommendation": blocking}

    def bound_report(self, root: Path, state: dict[str, object], role: str,
                     findings: list[dict[str, object]] | None = None) -> dict[str, object]:
        base, candidate = H["candidate_identity"](root, state.get("base_sha"))
        common = {"schema_version": "1.0", "status": "completed", "summary": "synthetic",
                  "attempt": state["attempt"], "base_sha": base, "candidate_id": candidate}
        if role in {"reviewer", "security_reviewer"}:
            return {**common, "findings": findings or [], "checks_reviewed": ["diff"],
                    "limitations": []}
        if role == "tester":
            return {**common, "outcome": "failed" if findings else "passed",
                    "commands": ["synthetic test"], "findings": findings or [], "limitations": []}
        if role == "verifier":
            return {**common, "classifications": [], "decision": "approved", "limitations": []}
        return {**common, "changed_files": [], "commands": [], "decisions": [],
                "limitations": [], "residual_risk": [], "blockers": []}

    def test_candidate_mismatch_rejected_for_all_read_only_roles(self) -> None:
        temp, root, _paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = {"attempt": 1, "base_sha": H["git_head"](root), "assessor_findings": {}}
        for role in ("reviewer", "tester", "security_reviewer", "verifier"):
            with self.subTest(role=role):
                report = self.bound_report(root, state, role)
                report["candidate_id"] = "candidate-v1:" + "0" * 64
                with self.assertRaisesRegex(HarnessError, "candidate identity"):
                    H["require_candidate_binding"](root, state, report, role)

    def test_assessor_accumulates_without_deciding_workflow(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "a" * 32
        state = H["load_state"](paths)
        state.update({"workflow_state": "ASSESSING", "attempt": 1,
                      "base_sha": H["git_head"](root), "completed_assessments": [],
                      "owner": {"generation": generation},
                      "worker": {"role": "reviewer", "generation": generation}})
        H["save_state"](paths, state)
        report = self.bound_report(root, state, "reviewer", [self.finding()])
        H["persist_worker_handoff"](
            paths, state, "reviewer", report, generation,
            paths["runtime"] / "reviewer.json", None,
        )
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "ASSESSING")
        self.assertEqual(final["completed_assessments"], ["reviewer"])

    def test_verifier_semantics_confirmed_rejected_and_inconclusive(self) -> None:
        temp, root, _paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        high = self.finding("R-1")
        low = self.finding("R-2", severity="low", blocking=False)
        state = {"attempt": 1, "base_sha": H["git_head"](root),
                 "assessor_findings": {"reviewer": [high, low]}}
        report = self.bound_report(root, state, "verifier")
        report["classifications"] = [
            {"finding_id": "R-1", "classification": "confirmed", "policy_blocking": True,
             "rationale": "confirmed", "evidence": []},
            {"finding_id": "R-2", "classification": "rejected", "policy_blocking": False,
             "rationale": "rejected", "evidence": []},
        ]
        report["decision"] = "changes_required"
        H["require_candidate_binding"](root, state, report, "verifier")
        report["classifications"][0]["classification"] = "inconclusive"
        report["decision"] = "approved"
        with self.assertRaisesRegex(HarnessError, "decision"):
            H["require_candidate_binding"](root, state, report, "verifier")
        report["decision"] = "blocked"
        H["require_candidate_binding"](root, state, report, "verifier")
        report["classifications"][0]["classification"] = "rejected"
        report["decision"] = "approved"
        H["require_candidate_binding"](root, state, report, "verifier")
        report["classifications"][0]["classification"] = "flaky_or_infra"
        with self.assertRaisesRegex(HarnessError, "decision"):
            H["require_candidate_binding"](root, state, report, "verifier")
        report["decision"] = "blocked"
        H["require_candidate_binding"](root, state, report, "verifier")
        report["decision"] = "approved"
        verifier_path = root / "flaky-verifier.json"
        H["atomic_json"](verifier_path, report)
        with self.assertRaisesRegex(HarnessError, "blocked decision"):
            H["validate_report"](verifier_path, H["schema_path"]("verifier"))

    def test_new_candidate_clears_all_assessor_and_verifier_evidence(self) -> None:
        temp, _root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        for key in ("trusted_implementation", "trusted_gates", "trusted_review",
                    "trusted_test", "trusted_security_review", "trusted_verification"):
            H["atomic_json"](paths[key], {"stale": True})
        for name in H["REPORT_FILES"].values():
            H["atomic_runtime_json"](paths["runtime"] / name, {"stale": True})
        H["clear_trusted_evidence"](paths)
        for key in ("trusted_implementation", "trusted_gates", "trusted_review",
                    "trusted_test", "trusted_security_review", "trusted_verification"):
            self.assertFalse(paths[key].exists())
        for name in H["REPORT_FILES"].values():
            self.assertFalse((paths["runtime"] / name).exists())

    def test_recovery_rejects_stale_binding_for_each_new_role(self) -> None:
        cases = (("reviewer", "generation"), ("tester", "attempt"),
                 ("security_reviewer", "candidate"), ("verifier", "candidate"))
        for role, mismatch in cases:
            with self.subTest(role=role, mismatch=mismatch):
                temp, root, paths = self.make_harness_repo()
                self.addCleanup(temp.cleanup)
                generation = "b" * 32
                base = H["git_head"](root)
                _, candidate = H["candidate_identity"](root, base)
                active = "VERIFYING" if role == "verifier" else "ASSESSING"
                output = paths["runtime"] / f"{role}-{generation}.json"
                state = H["load_state"](paths)
                state.update({"workflow_state": active, "attempt": 1, "base_sha": base,
                              "candidate_id": candidate,
                              "owner": {"pid": 999_999_999, "identity": "dead",
                                        "generation": generation},
                              "worker": {"role": role, "pid": None, "status": "completed",
                                         "generation": generation, "stage_id": state["stage_id"],
                                         "slice_id": state["slice_id"], "attempt": 1,
                                         "base_sha": base, "candidate_id": candidate,
                                         "expected_output": str(output)}})
                H["save_state"](paths, state)
                H["write_checkpoint"](paths, state, role, "completed", None, output,
                                      "recover", generation)
                checkpoint = H["load_json"](paths["trusted_checkpoint"])
                if mismatch == "generation":
                    checkpoint["generation"] = "c" * 32
                elif mismatch == "attempt":
                    checkpoint["attempt"] = 2
                else:
                    checkpoint["candidate_id"] = "candidate-v1:" + "0" * 64
                H["atomic_json"](paths["trusted_checkpoint"], checkpoint)
                with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
                    H["recover"](argparse.Namespace(retry=False, ack_human=False))
                self.assertEqual(H["load_state"](paths)["workflow_state"], "BLOCKED")
                self.assertFalse(paths["trusted_checkpoint"].exists())

    def test_tester_failure_and_flaky_infrastructure_are_distinct(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"attempt": 1, "base_sha": H["git_head"](root)})
        failed = self.bound_report(root, state, "tester", [self.finding("T-1")])
        flaky = self.bound_report(root, state, "tester")
        flaky["outcome"] = "flaky_or_infra"
        flaky["limitations"] = ["runner unavailable"]
        for name, value in (("failed", failed), ("flaky", flaky)):
            path = paths["runtime"] / f"{name}.json"
            H["atomic_json"](path, value)
            validated = H["validate_report"](path, H["schema_path"]("tester"))
            self.assertEqual(validated["outcome"], value["outcome"])

    def test_flaky_tester_handoff_blocks_without_completing_or_consuming_attempt(self) -> None:
        for findings in ([], [self.finding("T-INFRA")]):
            with self.subTest(findings=bool(findings)):
                temp, root, paths = self.make_harness_repo()
                self.addCleanup(temp.cleanup)
                generation = "a" * 32
                state = H["load_state"](paths)
                state.update({"workflow_state": "ASSESSING", "attempt": 1,
                              "base_sha": H["git_head"](root),
                              "completed_assessments": ["reviewer"],
                              "owner": {"generation": generation},
                              "worker": {"role": "tester", "generation": generation}})
                H["save_state"](paths, state)
                report = self.bound_report(root, state, "tester", findings)
                report.update({"outcome": "flaky_or_infra",
                               "limitations": ["test runner unavailable"]})
                H["persist_worker_handoff"](
                    paths, state, "tester", report, generation,
                    paths["runtime"] / "tester.json", None,
                )
                final = H["load_state"](paths)
                self.assertEqual(final["workflow_state"], "BLOCKED")
                self.assertEqual(final["attempt"], 1)
                self.assertNotIn("tester", final["completed_assessments"])
                self.assertEqual(final["tester_outcome"], "flaky_or_infra")
                with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
                    H["recover"](argparse.Namespace(
                        retry=True, ack_human=False, reopen_review=False,
                        reason=None, plan_file=None,
                    ))
                retried = H["load_state"](paths)
                self.assertEqual(retried["workflow_state"], "ASSESSING")
                self.assertEqual(retried["attempt"], 1)
                self.assertIsNone(retried["tester_outcome"])

    def test_blocked_flaky_tester_handoff_retries_same_candidate_assessment(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "a" * 32
        state = H["load_state"](paths)
        state.update({"workflow_state": "ASSESSING", "attempt": 1,
                      "base_sha": H["git_head"](root),
                      "completed_assessments": ["reviewer"],
                      "owner": {"generation": generation},
                      "worker": {"role": "tester", "generation": generation}})
        H["save_state"](paths, state)
        report = self.bound_report(root, state, "tester")
        report.update({"status": "blocked", "outcome": "flaky_or_infra",
                       "limitations": ["test runner unavailable"]})
        H["persist_worker_handoff"](
            paths, state, "tester", report, generation,
            paths["runtime"] / "tester.json", None,
        )
        blocked = H["load_state"](paths)
        self.assertEqual(blocked["workflow_state"], "BLOCKED")
        self.assertEqual(blocked["attempt"], 1)
        self.assertNotIn("tester", blocked["completed_assessments"])
        self.assertEqual(blocked["tester_outcome"], "flaky_or_infra")
        candidate_id = blocked["candidate_id"]
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(
                retry=True, ack_human=False, reopen_review=False,
                reason=None, plan_file=None,
            ))
        retried = H["load_state"](paths)
        self.assertEqual(retried["workflow_state"], "ASSESSING")
        self.assertEqual(retried["attempt"], 1)
        self.assertEqual(retried["candidate_id"], candidate_id)
        self.assertIsNone(retried["tester_outcome"])

    def test_non_infrastructure_blocked_tester_keeps_generic_recovery(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "a" * 32
        state = H["load_state"](paths)
        state.update({"workflow_state": "ASSESSING", "attempt": 1,
                      "base_sha": H["git_head"](root),
                      "owner": {"generation": generation},
                      "worker": {"role": "tester", "generation": generation}})
        H["save_state"](paths, state)
        report = self.bound_report(root, state, "tester")
        report.update({"status": "blocked", "limitations": ["evidence unavailable"]})
        H["persist_worker_handoff"](
            paths, state, "tester", report, generation,
            paths["runtime"] / "tester.json", None,
        )
        blocked = H["load_state"](paths)
        self.assertEqual(blocked["workflow_state"], "BLOCKED")
        self.assertIsNone(blocked["tester_outcome"])
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(
                retry=True, ack_human=False, reopen_review=False,
                reason=None, plan_file=None,
            ))
        retried = H["load_state"](paths)
        self.assertEqual(retried["workflow_state"], "CHANGES_REQUIRED")
        self.assertEqual(retried["attempt"], 1)

    def test_approval_rejects_flaky_tester_evidence_defense_in_depth(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"attempt": 1, "base_sha": H["git_head"](root)})
        implementation = self.bound_report(root, state, "implementer")
        review = self.bound_report(root, state, "reviewer")
        test = self.bound_report(root, state, "tester")
        test.update({"outcome": "flaky_or_infra", "limitations": ["runner unavailable"]})
        verification = self.bound_report(root, state, "verifier")
        candidate = implementation["candidate_id"]
        gates = {
            "schema_version": "1.0", "status": "passed", "level": "stage",
            "base_sha": state["base_sha"], "candidate_id": candidate,
            "failure_class": "none", "completed_at": "now",
            "gates": [{"name": "test", "status": "passed", "command": ["true"],
                       "resolved_command": ["/usr/bin/true"], "exit_code": 0,
                       "log": ".harness/runtime/gate.log"}],
        }
        for key, value in (("trusted_implementation", implementation),
                           ("trusted_gates", gates), ("trusted_review", review),
                           ("trusted_test", test), ("trusted_verification", verification)):
            H["atomic_json"](paths[key], value)
        state.update({
            "workflow_state": "APPROVED", "candidate_id": candidate,
            "gates_passed": True, "gate_level": "stage",
            "completed_assessments": ["reviewer", "tester"],
            "assessor_findings": {"reviewer": [], "tester": []},
            "verifier_state": "approved",
            "validated_worktree": H["worktree_fingerprint"](root),
            "owner": None, "worker": None,
        })
        H["save_state"](paths, state)
        with contextlib.chdir(root), self.assertRaisesRegex(HarnessError, "flaky_or_infra"):
            H["approve_slice"](argparse.Namespace(
                complete_stage=True, next_slice=None, plan_file=None
            ))

    def test_security_policy_and_verifier_join_are_fail_closed(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        self.assertEqual(state["required_assessments"], ["reviewer", "tester"])
        state.update({"workflow_state": "ASSESSING", "attempt": 1,
                      "base_sha": H["git_head"](root), "gates_passed": True,
                      "gate_level": "slice", "completed_assessments": ["reviewer"]})
        H["save_state"](paths, state)
        with contextlib.chdir(root), self.assertRaisesRegex(HarnessError, "all assessor"):
            H["run_worker"](argparse.Namespace(dry_run=False), "verifier")
        with contextlib.chdir(root), self.assertRaisesRegex(HarnessError, "only by SECURITY"):
            H["run_worker"](argparse.Namespace(dry_run=False), "security_reviewer")

    def test_security_workflow_requires_security_reviewer(self) -> None:
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["init_project"](argparse.Namespace())
        plan = root / "plan.md"
        plan.write_text("# Security plan\n\nVerify explicit Security Reviewer policy selection.\n",
                        encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", ".harness", "plan.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initialize harness"], cwd=root, check=True)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["start_stage"](argparse.Namespace(
                stage="S1", title="Security stage", slice="S1.1",
                plan_file="plan.md", workflow="SECURITY",
            ))
        state = H["load_state"](H["paths"](root))
        self.assertEqual(state["required_assessments"],
                         ["reviewer", "tester", "security_reviewer"])

    def test_all_role_dry_runs_preserve_business_state(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        before = H["load_state"](paths)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            for role in ("implementer", "reviewer", "tester", "security_reviewer", "verifier"):
                H["run_worker"](argparse.Namespace(dry_run=True), role)
        self.assertEqual(H["load_state"](paths), before)
        for role in ("implementer", "reviewer", "tester", "security_reviewer", "verifier"):
            self.assertFalse(paths["trusted"] .joinpath(H["REPORT_FILES"][role]).exists())

    def test_state_machine_docs_match_recovery_edge(self) -> None:
        self.assertNotIn("VALIDATING", H["TRANSITIONS"]["CHANGES_REQUIRED"])
        docs = (SKILL_ROOT / "references/state-machine.md").read_text(encoding="utf-8")
        self.assertIn("CHANGES_REQUIRED -> IMPLEMENTING | HUMAN_CHECKPOINT", docs)

    def test_current_stage_serialization_has_one_terminal_newline(self) -> None:
        temp, _root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        value = paths["stage"].read_text(encoding="utf-8")
        self.assertTrue(value.endswith("\n"))
        self.assertFalse(value.endswith("\n\n"))

    def test_clear_trusted_evidence_invalidates_previous_checkpoint(self) -> None:
        temp, _root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        for key in (
            "trusted_implementation", "trusted_gates", "trusted_review",
            "trusted_checkpoint", "checkpoint",
        ):
            H["atomic_json"](paths[key], {"stale": True})
        H["clear_trusted_evidence"](paths)
        for key in (
            "trusted_implementation", "trusted_gates", "trusted_review",
            "trusted_checkpoint", "checkpoint",
        ):
            self.assertFalse(paths[key].exists(), key)

    def test_start_stage_synchronizes_project_current_status(self) -> None:
        temp, _root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        project = paths["project"].read_text(encoding="utf-8")
        self.assertIn("Active Stage S1 / Slice S1.1: Test stage.", project)
        self.assertNotIn("Harness initialized; no Stage approved.", project)

    def test_sync_project_state_migrates_legacy_status_fail_closed(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        project = paths["project"].read_text(encoding="utf-8")
        project = project.replace(
            "Active Stage S1 / Slice S1.1: Test stage.",
            "Harness initialized; no Stage approved.",
        )
        H["atomic_write"](paths["project"], project)
        state = H["load_state"](paths)
        cfg = H["load_config"](paths)
        state["protected_baseline"] = H["protected_snapshot"](root, cfg)
        H["save_state"](paths, state)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["sync_project_state"](argparse.Namespace())
        migrated = paths["project"].read_text(encoding="utf-8")
        self.assertIn("Active Stage S1 / Slice S1.1: Test stage.", migrated)
        self.assertEqual(
            H["load_state"](paths)["protected_baseline"],
            H["protected_snapshot"](root, cfg),
        )

    def test_harness_gate_failure_stays_validating_for_direct_retry(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        config = paths["trusted_config"].read_text(encoding="utf-8")
        config = config.replace("fast = []", 'fast = [["true"]]', 1)
        H["atomic_write"](paths["trusted_config"], config, mode=0o600)
        state = H["load_state"](paths)
        state["workflow_state"] = "VALIDATING"
        H["save_state"](paths, state)
        with contextlib.chdir(root), mock.patch.dict(
                H, {"run_logged": mock.Mock(side_effect=HarnessError("spawn failed"))}):
            with self.assertRaises(HarnessError):
                H["run_gates"](argparse.Namespace(level="fast"))
        self.assertEqual(H["load_state"](paths)["workflow_state"], "VALIDATING")
        self.assertFalse(paths["trusted_gates"].exists())

        with contextlib.chdir(root), mock.patch.dict(H, {"run_logged": lambda *_a, **_k: (0, False)}), \
                contextlib.redirect_stdout(io.StringIO()):
            H["run_gates"](argparse.Namespace(level="fast"))
        self.assertTrue(H["load_state"](paths)["gates_passed"])

    def test_gate_report_records_resolved_executable(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        config = paths["trusted_config"].read_text(encoding="utf-8")
        config = config.replace("fast = []", 'fast = [["true"]]', 1)
        H["atomic_write"](paths["trusted_config"], config, mode=0o600)
        state = H["load_state"](paths)
        state["workflow_state"] = "VALIDATING"
        H["save_state"](paths, state)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["run_gates"](argparse.Namespace(level="fast"))
        report = H["load_json"](paths["trusted_gates"])
        gate = report["gates"][0]
        self.assertEqual(gate["command"], ["true"])
        self.assertTrue(Path(gate["resolved_command"][0]).is_absolute())
        self.assertEqual(gate["exit_code"], 0)

    def test_gate_timeout_is_product_failure_not_retry_privilege(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        config = paths["trusted_config"].read_text(encoding="utf-8")
        config = config.replace("fast = []", 'fast = [["true"]]', 1)
        H["atomic_write"](paths["trusted_config"], config, mode=0o600)
        state = H["load_state"](paths)
        state["workflow_state"] = "VALIDATING"
        H["save_state"](paths, state)
        with contextlib.chdir(root), mock.patch.dict(H, {"run_logged": lambda *_a, **_k: (124, True)}):
            with self.assertRaises(HarnessError), contextlib.redirect_stdout(io.StringIO()):
                H["run_gates"](argparse.Namespace(level="fast"))
        report = H["load_json"](paths["trusted_gates"])
        self.assertEqual(report["failure_class"], "product")
        self.assertTrue(report["gates"][0]["timed_out"])
        self.assertEqual(H["load_state"](paths)["workflow_state"], "CHANGES_REQUIRED")

    def test_product_gate_failure_cannot_retry_without_implementer(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"workflow_state": "CHANGES_REQUIRED", "attempt": 1,
                      "owner": None, "worker": None})
        H["save_state"](paths, state)
        H["atomic_json"](paths["trusted_implementation"], {
            "status": "completed", "summary": "done", "changed_files": [],
            "tests": [], "blockers": [],
        })
        report = {
            # Legacy failed reports omitted failure_class; migration must treat
            # them as product failures, never as environment-retry evidence.
            "status": "failed", "level": "slice",
            "completed_at": "now",
            "gates": [{"name": "test", "status": "failed", "command": ["false"],
                       "resolved_command": ["/usr/bin/false"], "exit_code": 1,
                       "log": ".harness/runtime/test.log"}],
        }
        H["atomic_json"](paths["trusted_gates"], report)
        with contextlib.chdir(root), self.assertRaises(HarnessError):
            H["recover"](argparse.Namespace(retry=True, ack_human=False))
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "CHANGES_REQUIRED")
        self.assertTrue(paths["trusted_gates"].exists())
        self.assertTrue(paths["trusted_implementation"].exists())

    def test_gate_recovery_cannot_bypass_reviewer_findings(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"workflow_state": "CHANGES_REQUIRED", "attempt": 1,
                      "owner": None, "worker": None})
        H["save_state"](paths, state)
        H["atomic_json"](paths["trusted_implementation"], {
            "status": "completed", "summary": "done", "changed_files": [],
            "tests": [], "blockers": [],
        })
        H["atomic_json"](paths["trusted_gates"], {
            "status": "failed", "level": "slice", "failure_class": "product",
            "completed_at": "now",
            "gates": [{"name": "test", "status": "failed", "command": ["false"],
                       "resolved_command": ["/usr/bin/false"], "exit_code": 1,
                       "log": ".harness/runtime/test.log"}],
        })
        H["atomic_json"](paths["trusted_review"], {
            "verdict": "changes_required", "summary": "fix it",
            "findings": [{"severity": "high", "file": "x", "line": 1,
                          "message": "product finding"}], "checks_reviewed": ["diff"],
        })
        with contextlib.chdir(root), self.assertRaises(HarnessError):
            H["recover"](argparse.Namespace(retry=True, ack_human=False))
        self.assertEqual(H["load_state"](paths)["workflow_state"], "CHANGES_REQUIRED")
        self.assertTrue(paths["trusted_gates"].exists())
        self.assertTrue(paths["trusted_review"].exists())

    def test_attempt_cap_enters_human_checkpoint_before_launch(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"workflow_state": "SLICE_READY", "attempt": 3,
                      "owner": None, "worker": None})
        H["save_state"](paths, state)
        with contextlib.chdir(root), self.assertRaises(HarnessError):
            H["run_worker"](argparse.Namespace(dry_run=False), "implementer")
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "HUMAN_CHECKPOINT")
        self.assertEqual(final["attempt"], 3)
        self.assertEqual(final.get("worker_run_seq", 0), 0)

    def test_ack_human_can_amend_the_trusted_plan_and_rebind_protection(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        amended = root / "amended-plan.md"
        amended.write_text(
            "# Amended plan\n\nRetain evidence required by the checkpoint review.\n",
            encoding="utf-8",
        )
        state = H["load_state"](paths)
        state.update({"workflow_state": "HUMAN_CHECKPOINT", "attempt": 3,
                      "owner": None, "worker": None})
        H["save_state"](paths, state)

        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(
                retry=False, ack_human=True, plan_file="amended-plan.md"
            ))

        final = H["load_state"](paths)
        current = paths["stage"].read_text(encoding="utf-8")
        trusted = paths["trusted_stage"].read_text(encoding="utf-8")
        self.assertEqual(final["workflow_state"], "SLICE_READY")
        self.assertEqual(final["attempt"], 0)
        self.assertEqual(final["checkpoint_epoch"], 1)
        self.assertEqual(current, trusted)
        self.assertIn("# Amended plan", trusted)
        self.assertEqual(
            final["protected_baseline"],
            H["protected_snapshot"](root, H["load_config"](paths)),
        )

    def test_plan_amendment_requires_human_checkpoint_acknowledgement(self) -> None:
        temp, root, _paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        amended = root / "amended-plan.md"
        amended.write_text("# Not approved\n", encoding="utf-8")

        with contextlib.chdir(root), self.assertRaisesRegex(
            HarnessError, "plan amendment requires --ack-human"
        ):
            H["recover"](argparse.Namespace(
                retry=False, ack_human=False, plan_file="amended-plan.md"
            ))

    def test_approved_review_can_be_reopened_with_an_operator_reason(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({
            "workflow_state": "APPROVED",
            "gates_passed": True,
            "gate_level": "slice",
            "verifier_state": "approved",
            "validated_worktree": "stale-fingerprint",
            "unresolved_findings": [],
        })
        H["save_state"](paths, state)

        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(
                retry=False,
                ack_human=False,
                reopen_review=True,
                reason="A pure Stage gate is required before final approval.",
                plan_file=None,
            ))

        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "CHANGES_REQUIRED")
        self.assertFalse(final["gates_passed"])
        self.assertIsNone(final["gate_level"])
        self.assertEqual(final["verifier_state"], "pending")
        self.assertIsNone(final["validated_worktree"])
        self.assertEqual(final["unresolved_findings"], [{
            "severity": "medium",
            "file": "<orchestrator>",
            "line": 1,
            "message": "A pure Stage gate is required before final approval.",
        }])

    def test_infrastructure_launch_failure_does_not_consume_attempt(self) -> None:
        temp, root, paths = self.make_harness_repo(
            adapter_argv='["/definitely/missing/agent-adapter"]'
        )
        self.addCleanup(temp.cleanup)
        with contextlib.chdir(root), self.assertRaises(HarnessError):
            H["run_worker"](argparse.Namespace(dry_run=False), "implementer")
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "BLOCKED")
        self.assertEqual(final["attempt"], 0)
        self.assertEqual(final["worker_run_seq"], 1)
        self.assertFalse(final["worker"]["business_attempt_committed"])
        checkpoint = H["load_json"](paths["trusted_checkpoint"])
        self.assertEqual(checkpoint["attempt"], 0)
        self.assertEqual(checkpoint["worker_status"], "failed")

    def test_invalid_and_missing_handoff_charge_and_synchronize_checkpoint(self) -> None:
        for reason in ("invalid structured handoff", "missing structured handoff"):
            with self.subTest(reason=reason):
                temp, _root, paths = self.make_harness_repo()
                self.addCleanup(temp.cleanup)
                generation = "b" * 32
                report = paths["runtime"] / f"implementer-{generation}.json"
                state = H["load_state"](paths)
                worker = {
                    "role": "implementer", "pid": None, "status": "completed",
                    "generation": generation, "stage_id": state["stage_id"],
                    "slice_id": state["slice_id"], "attempt": 1,
                    "expected_output": str(report),
                }
                state.update({
                    "workflow_state": "IMPLEMENTING", "attempt": 1,
                    "worker_run_seq": 1,
                    "owner": {"pid": os.getpid(), "identity": H["process_identity"](os.getpid()),
                              "generation": generation, "claimed_at": "now"},
                    "worker": worker,
                })
                H["save_state"](paths, state)
                H["write_checkpoint"](
                    paths, state, "implementer", "completed", None, report,
                    "run-gates", generation,
                )
                H["persist_blocked_worker"](
                    paths, state, "implementer", worker, reason,
                    generation, report, None,
                )
                final = H["load_state"](paths)
                checkpoint = H["load_json"](paths["trusted_checkpoint"])
                self.assertEqual(final["workflow_state"], "BLOCKED")
                self.assertEqual(final["attempt"], 1)
                self.assertEqual(checkpoint["attempt"], 1)
                self.assertEqual(checkpoint["worker_status"], "failed")
                self.assertEqual(checkpoint["worker_run_seq"], 1)

    def test_only_recognized_unchanged_startup_failure_releases_attempt(self) -> None:
        cases = (
            ("spawn_exec_failure", False, 0),
            ("agent_output_schema_startup_rejection", False, 0),
            (None, False, 1),
            ("agent_output_schema_startup_rejection", True, 1),
        )
        for failure_class, change_worktree, expected_attempt in cases:
            with self.subTest(failure_class=failure_class, changed=change_worktree):
                temp, root, paths = self.make_harness_repo()
                self.addCleanup(temp.cleanup)
                generation = "9" * 32
                report = paths["runtime"] / f"implementer-{generation}.json"
                state = H["load_state"](paths)
                worker = {
                    "role": "implementer", "pid": None, "status": "running",
                    "generation": generation, "stage_id": state["stage_id"],
                    "slice_id": state["slice_id"], "attempt": 1,
                    "base_sha": H["git_head"](root), "candidate_id": None,
                    "launch_worktree_fingerprint": H["worktree_fingerprint"](root),
                    "expected_output": str(report),
                }
                state.update({
                    "workflow_state": "IMPLEMENTING", "attempt": 1,
                    "worker_run_seq": 1, "base_sha": worker["base_sha"],
                    "owner": {"pid": os.getpid(),
                              "identity": H["process_identity"](os.getpid()),
                              "generation": generation, "claimed_at": "now"},
                    "worker": worker,
                })
                H["save_state"](paths, state)
                if change_worktree:
                    (root / "tracked.txt").write_text("business work\n", encoding="utf-8")
                H["persist_blocked_worker"](
                    paths, state, "implementer", worker, "synthetic failure",
                    generation, report, None, failure_class=failure_class, root=root,
                )
                final = H["load_state"](paths)
                checkpoint = H["load_json"](paths["trusted_checkpoint"])
                self.assertEqual(final["attempt"], expected_attempt)
                self.assertEqual(checkpoint["attempt"], expected_attempt)
                self.assertEqual(final["worker"]["business_attempt_committed"],
                                 expected_attempt == 1)

    def test_timeout_and_changed_worktree_exit_one_remain_charged(self) -> None:
        for reason, change_worktree in (("Implementer timed out", False),
                                        ("Implementer exited 1", True)):
            with self.subTest(reason=reason):
                temp, root, paths = self.make_harness_repo()
                self.addCleanup(temp.cleanup)
                generation = "8" * 32
                report = paths["runtime"] / f"implementer-{generation}.json"
                state = H["load_state"](paths)
                worker = {
                    "role": "implementer", "pid": None, "status": "running",
                    "generation": generation, "stage_id": state["stage_id"],
                    "slice_id": state["slice_id"], "attempt": 1,
                    "base_sha": H["git_head"](root), "candidate_id": None,
                    "launch_worktree_fingerprint": H["worktree_fingerprint"](root),
                    "expected_output": str(report),
                }
                state.update({
                    "workflow_state": "IMPLEMENTING", "attempt": 1,
                    "worker_run_seq": 1, "base_sha": worker["base_sha"],
                    "owner": {"pid": os.getpid(),
                              "identity": H["process_identity"](os.getpid()),
                              "generation": generation, "claimed_at": "now"},
                    "worker": worker,
                })
                H["save_state"](paths, state)
                if change_worktree:
                    (root / "tracked.txt").write_text("changed then failed\n", encoding="utf-8")
                H["persist_blocked_worker"](
                    paths, state, "implementer", worker, reason,
                    generation, report, None, root=root,
                )
                self.assertEqual(H["load_state"](paths)["attempt"], 1)
                self.assertEqual(H["load_json"](paths["trusted_checkpoint"])["attempt"], 1)

    def test_schema_startup_rejection_recognition_is_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = Path(raw) / "report.json"
            self.assertTrue(H["agent_schema_startup_rejection"](
                "Invalid schema for response_format: unsupported keyword", report,
            ))
            self.assertFalse(H["agent_schema_startup_rejection"](
                "output schema validation failed after work", report,
            ))
            report.write_text("{}", encoding="utf-8")
            self.assertFalse(H["agent_schema_startup_rejection"](
                "Invalid schema for response_format", report,
            ))

    def test_checkpoint_write_failure_invalidates_stale_checkpoint(self) -> None:
        temp, _root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "c" * 32
        report = paths["runtime"] / f"implementer-{generation}.json"
        state = H["load_state"](paths)
        worker = {
            "role": "implementer", "pid": None, "status": "launching",
            "generation": generation, "stage_id": state["stage_id"],
            "slice_id": state["slice_id"], "attempt": 1,
            "expected_output": str(report),
        }
        state.update({
            "workflow_state": "IMPLEMENTING", "attempt": 1, "worker_run_seq": 1,
            "owner": {"pid": os.getpid(), "identity": H["process_identity"](os.getpid()),
                      "generation": generation, "claimed_at": "now"},
            "worker": worker,
        })
        H["save_state"](paths, state)
        H["atomic_json"](paths["trusted_checkpoint"], {"generation": generation, "attempt": 1})
        with mock.patch.dict(H, {"write_checkpoint": mock.Mock(side_effect=HarnessError("write failed"))}), \
                self.assertRaises(HarnessError):
            H["persist_blocked_worker"](
                paths, state, "implementer", worker, "persistence failed",
                generation, report, None,
            )
        self.assertFalse(paths["trusted_checkpoint"].exists())
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "BLOCKED")
        self.assertEqual(final["attempt"], 1)

    def test_recover_accepts_valid_blocked_implementer_handoff(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "d" * 32
        report = paths["runtime"] / f"implementer-{generation}.json"
        state = H["load_state"](paths)
        state.update({"attempt": 1, "base_sha": H["git_head"](root)})
        result = self.bound_report(root, state, "implementer")
        result.update({"status": "blocked", "blockers": ["tool unavailable"]})
        report.write_text(json.dumps(result), encoding="utf-8")
        worker = {
            "role": "implementer", "pid": None, "status": "completed",
            "generation": generation, "stage_id": state["stage_id"],
            "slice_id": state["slice_id"], "attempt": 1,
            "base_sha": state["base_sha"], "candidate_id": None,
            "expected_output": str(report),
        }
        state.update({
            "workflow_state": "IMPLEMENTING", "attempt": 1, "worker_run_seq": 1,
            "owner": {"pid": 999_999_999, "identity": "dead-owner",
                      "generation": generation, "claimed_at": "now"},
            "worker": worker,
        })
        H["save_state"](paths, state)
        H["write_checkpoint"](
            paths, state, "implementer", "completed", None, report,
            "run-gates", generation,
        )
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(retry=False, ack_human=False))
        final = H["load_state"](paths)
        checkpoint = H["load_json"](paths["trusted_checkpoint"])
        trusted = H["load_json"](paths["trusted_implementation"])
        self.assertEqual(final["workflow_state"], "BLOCKED")
        self.assertEqual(final["attempt"], 1)
        self.assertEqual(trusted["status"], "blocked")
        self.assertEqual(checkpoint["worker_status"], "completed")
        self.assertEqual(checkpoint["generation"], final["worker"]["generation"])
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()) as captured, \
                mock.patch.dict(H, {"owner_alive": lambda _owner: False}):
            H["recover"](argparse.Namespace(retry=False, ack_human=False))
        self.assertIn("handoff-found", captured.getvalue())
        self.assertEqual(H["load_state"](paths)["workflow_state"], "BLOCKED")

    def test_approved_reviewer_handoff_synchronizes_checkpoint(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "d" * 32
        report = paths["runtime"] / f"reviewer-{generation}.json"
        state = H["load_state"](paths)
        state.update({"attempt": 1, "base_sha": H["git_head"](root)})
        result = self.bound_report(root, state, "reviewer")
        state["candidate_id"] = result["candidate_id"]
        state.update({
            "workflow_state": "ASSESSING", "attempt": 1,
            "unresolved_findings": [{
                "severity": "high", "file": "stale", "line": 1,
                "message": "resolved finding",
            }],
            "owner": {
                "pid": os.getpid(), "identity": H["process_identity"](os.getpid()),
                "generation": generation, "claimed_at": "now",
            },
            "worker": {
                "role": "reviewer", "pid": None, "status": "running",
                "generation": generation, "stage_id": state["stage_id"],
                "slice_id": state["slice_id"], "attempt": 1,
                "base_sha": state["base_sha"], "candidate_id": result["candidate_id"],
                "expected_output": str(report),
            },
        })
        H["save_state"](paths, state)
        with contextlib.chdir(root):
            H["persist_worker_handoff"](
                paths, state, "reviewer", result, generation, report, None,
            )
        final = H["load_state"](paths)
        checkpoint = H["load_json"](paths["trusted_checkpoint"])
        self.assertIn("reviewer", final["completed_assessments"])
        self.assertEqual(final["worker"]["generation"], generation)
        self.assertEqual(checkpoint["workflow_state"], "ASSESSING")
        self.assertEqual(checkpoint["unresolved_findings"], [])
        self.assertEqual(checkpoint["worker_status"], "completed")

    def test_repeated_recover_preserves_reviewer_approval(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "f" * 32
        report = paths["runtime"] / f"reviewer-{generation}.json"
        state = H["load_state"](paths)
        state.update({"attempt": 1, "base_sha": H["git_head"](root)})
        result = self.bound_report(root, state, "reviewer")
        state["candidate_id"] = result["candidate_id"]
        report.write_text(json.dumps(result), encoding="utf-8")
        state.update({
            "workflow_state": "ASSESSING", "attempt": 1, "worker_run_seq": 1,
            "owner": {"pid": 999_999_999, "identity": "dead-owner",
                      "generation": generation, "claimed_at": "now"},
            "worker": {
                "role": "reviewer", "pid": None, "status": "completed",
                "generation": generation, "stage_id": state["stage_id"],
                "slice_id": state["slice_id"], "attempt": 1,
                "base_sha": state["base_sha"], "candidate_id": result["candidate_id"],
                "expected_output": str(report),
            },
        })
        H["save_state"](paths, state)
        H["write_checkpoint"](
            paths, state, "reviewer", "completed", None, report,
            "approve-slice", generation,
        )
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(retry=False, ack_human=False))
        first = H["load_state"](paths)
        self.assertEqual(first["workflow_state"], "ASSESSING")
        self.assertIn("reviewer", first["completed_assessments"])
        self.assertEqual(first["worker"]["generation"], generation)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(H, {"owner_alive": lambda _owner: False}):
            H["recover"](argparse.Namespace(retry=False, ack_human=False))
        second = H["load_state"](paths)
        self.assertEqual(second["workflow_state"], "ASSESSING")
        self.assertIn("reviewer", second["completed_assessments"])
        self.assertEqual(second["worker"]["generation"], generation)
        self.assertEqual(H["load_json"](paths["trusted_review"])["status"], "completed")

    def test_recover_checkpoint_mismatch_charges_attempt_and_invalidates_checkpoint(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "e" * 32
        report = paths["runtime"] / f"implementer-{generation}.json"
        state = H["load_state"](paths)
        state.update({
            "workflow_state": "IMPLEMENTING", "attempt": 1, "worker_run_seq": 1,
            "owner": {"pid": 999_999_999, "identity": "dead-owner",
                      "generation": generation, "claimed_at": "now"},
            "worker": {
                "role": "implementer", "pid": None, "process_identity": None,
                "status": "completed", "generation": generation,
                "stage_id": state["stage_id"], "slice_id": state["slice_id"],
                "attempt": 1, "expected_output": str(report),
            },
        })
        H["save_state"](paths, state)
        H["atomic_json"](paths["trusted_checkpoint"], {
            "worker_role": "implementer", "generation": "stale",
            "stage_id": state["stage_id"], "slice_id": state["slice_id"],
            "attempt": 1, "process_id": None, "process_identity": None,
        })
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(retry=False, ack_human=False))
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "BLOCKED")
        self.assertEqual(final["attempt"], 1)
        self.assertFalse(paths["trusted_checkpoint"].exists())
        self.assertFalse(paths["checkpoint"].exists())

    def test_background_session_binding_is_generation_scoped_and_persistent(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "a" * 32
        state = H["load_state"](paths)
        identity = H["process_identity"](os.getpid())
        state.update({
            "workflow_state": "IMPLEMENTING", "attempt": 1,
            "owner": {"pid": os.getpid(), "identity": identity,
                      "generation": generation, "claimed_at": "now"},
            "worker": {"role": "implementer", "pid": None, "status": "launching",
                       "generation": generation, "stage_id": "S1", "slice_id": "S1.1",
                       "attempt": 1, "expected_output": str(paths["runtime"] / "output.json")},
        })
        H["save_state"](paths, state)
        H["atomic_json"](paths["trusted_checkpoint"], {
            "generation": generation, "worker_session_id": None,
        })
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["bind_session"](argparse.Namespace(
                generation=generation, session_id="proc_test123"
            ))
        bound = H["load_state"](paths)
        self.assertEqual(bound["worker"]["session_id"], "proc_test123")
        report = paths["runtime"] / "implementer-output.json"
        H["write_checkpoint"](
            paths, bound, "implementer", "running", None, report,
            "run-gates", generation,
        )
        self.assertEqual(H["load_json"](paths["trusted_checkpoint"])["worker_session_id"],
                         "proc_test123")
        with contextlib.chdir(root), self.assertRaises(HarnessError):
            H["bind_session"](argparse.Namespace(
                generation="b" * 32, session_id="proc_stale"
            ))

    def test_stage_completion_replaces_pending_canonical_stage_document(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"attempt": 1, "base_sha": H["git_head"](root)})
        implementation = self.bound_report(root, state, "implementer")
        review = self.bound_report(root, state, "reviewer")
        test = self.bound_report(root, state, "tester")
        verification = self.bound_report(root, state, "verifier")
        candidate = implementation["candidate_id"]
        gates = {
            "schema_version": "1.0", "status": "passed", "level": "stage",
            "base_sha": state["base_sha"], "candidate_id": candidate, "failure_class": "none",
            "completed_at": "now",
            "gates": [{"name": "test", "status": "passed", "command": ["true"],
                       "resolved_command": ["/usr/bin/true"], "exit_code": 0,
                       "log": ".harness/runtime/gate.log"}],
        }
        H["atomic_json"](paths["trusted_implementation"], implementation)
        H["atomic_json"](paths["trusted_gates"], gates)
        H["atomic_json"](paths["trusted_review"], review)
        H["atomic_json"](paths["trusted_test"], test)
        H["atomic_json"](paths["trusted_verification"], verification)
        state.update({
            "workflow_state": "APPROVED", "attempt": 1,
            "candidate_id": candidate, "gates_passed": True, "gate_level": "stage",
            "completed_assessments": ["reviewer", "tester"], "verifier_state": "approved",
            "validated_worktree": H["worktree_fingerprint"](root),
            "owner": None, "worker": None,
        })
        H["save_state"](paths, state)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["approve_slice"](argparse.Namespace(
                complete_stage=True, next_slice=None, plan_file=None
            ))
        final = H["load_state"](paths)
        current = paths["stage"].read_text(encoding="utf-8")
        trusted = paths["trusted_stage"].read_text(encoding="utf-8")
        self.assertEqual(final["workflow_state"], "STAGE_COMPLETED")
        self.assertEqual(current, trusted)
        self.assertIn("- Status: Completed", current)
        self.assertNotIn("[ ]", current)
        self.assertNotIn("remain pending", current.lower())

        stale = "# Current Stage\n\n- Status: Pending\n\n- [ ] Final review remains pending.\n"
        H["atomic_write"](paths["stage"], stale)
        H["atomic_write"](paths["trusted_stage"], stale, mode=0o600)
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            H["recover"](argparse.Namespace(retry=False, ack_human=False))
        repaired = paths["stage"].read_text(encoding="utf-8")
        self.assertIn("- Status: Completed", repaired)
        self.assertNotIn("[ ]", repaired)
        self.assertEqual(repaired, paths["trusted_stage"].read_text(encoding="utf-8"))

    def test_volatile_git_metadata_is_ignored(self) -> None:
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        cfg = {"protected_paths": [".git/", "protected.txt"]}
        baseline = H["protected_snapshot"](root, cfg)
        # Simulate a baseline written by an older Harness that scanned volatile metadata.
        baseline["git_metadata"]["index"] = {"kind": "sha256", "digest": "obsolete"}
        (root / "untracked.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "add", "untracked.txt"], cwd=root, check=True)
        H["require_protected_unchanged"](root, cfg, {"protected_baseline": baseline})

    def test_protected_content_head_and_config_changes_are_rejected(self) -> None:
        cfg = {"protected_paths": [".git/", "protected.txt"]}

        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        baseline = H["protected_snapshot"](root, cfg)
        (root / "protected.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(HarnessError):
            H["require_protected_unchanged"](root, cfg, {"protected_baseline": baseline})

        temp2, root2 = self.make_repo()
        self.addCleanup(temp2.cleanup)
        baseline2 = H["protected_snapshot"](root2, cfg)
        subprocess.run(["git", "config", "harness.test", "changed"], cwd=root2, check=True)
        with self.assertRaises(HarnessError):
            H["require_protected_unchanged"](root2, cfg, {"protected_baseline": baseline2})

        temp3, root3 = self.make_repo()
        self.addCleanup(temp3.cleanup)
        baseline3 = H["protected_snapshot"](root3, cfg)
        (root3 / "tracked.txt").write_text("next\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root3, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "next"], cwd=root3, check=True)
        with self.assertRaises(HarnessError):
            H["require_protected_unchanged"](root3, cfg, {"protected_baseline": baseline3})

    def test_protected_snapshot_covers_dotgit_prefix_directories_and_secret_metadata(self) -> None:
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        workflow = root / ".github/workflows/ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: before\n", encoding="utf-8")
        protected_dir = root / "protected-dir"
        protected_dir.mkdir()
        (protected_dir / "nested.txt").write_text("before\n", encoding="utf-8")
        dotgitignore = root / ".gitignore"
        dotgitignore.write_text("before\n", encoding="utf-8")
        secret = root / ".env"
        secret.write_text("AAAA\n", encoding="utf-8")
        original = secret.stat()
        cfg = {"protected_paths": [
            ".github/workflows/ci.yml", ".gitignore", "protected-dir", ".env",
        ]}
        baseline = H["protected_snapshot"](root, cfg)
        self.assertIn(".github/workflows/ci.yml", baseline["files"])
        self.assertIn(".gitignore", baseline["files"])
        self.assertIn("protected-dir/nested.txt", baseline["files"])
        self.assertIn("ctime_ns", baseline["files"][".env"])

        workflow.write_text("name: after!\n", encoding="utf-8")
        (protected_dir / "nested.txt").write_text("after\n", encoding="utf-8")
        dotgitignore.write_text("after!\n", encoding="utf-8")
        secret.write_text("BBBB\n", encoding="utf-8")
        os.utime(secret, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(HarnessError):
            H["require_protected_unchanged"](root, cfg, {"protected_baseline": baseline})

    def test_protected_snapshot_rejects_symlinks_and_special_files(self) -> None:
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        target = root / "target.txt"
        target.write_text("content\n", encoding="utf-8")
        symlink = root / "protected-link"
        symlink.symlink_to(target)
        with self.assertRaises(HarnessError):
            H["protected_snapshot"](root, {"protected_paths": ["protected-link"]})

        fifo = root / "protected-fifo"
        os.mkfifo(fifo)
        with self.assertRaises(HarnessError):
            H["protected_snapshot"](root, {"protected_paths": ["protected-fifo"]})

        outside = root.parent / f"outside-{root.name}"
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text("outside-secret", encoding="utf-8")
        intermediate = root / "linked-dir"
        intermediate.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(HarnessError, "must not contain a symlink"):
            H["protected_snapshot"](
                root, {"protected_paths": ["linked-dir/secret.txt"]},
            )
        with self.assertRaisesRegex(HarnessError, "must not contain a symlink"):
            H["protected_snapshot"](
                root, {"protected_paths": ["linked-dir/*.txt"]},
            )
        with self.assertRaisesRegex(HarnessError, "final path component"):
            H["protected_snapshot"](
                root, {"protected_paths": ["*/secret.txt"]},
            )

    def test_worktree_fingerprint_detects_same_size_secret_mutation(self) -> None:
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        secret = root / "token.txt"
        secret.write_text("AAAA\n", encoding="utf-8")
        original = secret.stat()
        before = H["worktree_fingerprint"](root)
        secret.write_text("BBBB\n", encoding="utf-8")
        os.utime(secret, ns=(original.st_atime_ns, original.st_mtime_ns))
        after = H["worktree_fingerprint"](root)
        self.assertNotEqual(before, after)

    def test_worktree_fingerprint_binds_submodule_head_and_dirty_state(self) -> None:
        source_temp, source = self.make_repo()
        self.addCleanup(source_temp.cleanup)
        first = H["git_head"](source)
        (source / "tracked.txt").write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "second"], cwd=source, check=True)
        second = H["git_head"](source)
        (source / "tracked.txt").write_text("third\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "third"], cwd=source, check=True)
        third = H["git_head"](source)

        parent_temp, parent = self.make_repo()
        self.addCleanup(parent_temp.cleanup)
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
             str(source), "vendor/module"], cwd=parent, check=True,
        )
        module = parent / "vendor" / "module"
        subprocess.run(["git", "checkout", "-q", first], cwd=module, check=True)
        subprocess.run(["git", "add", ".gitmodules", "vendor/module"], cwd=parent, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add submodule"], cwd=parent, check=True)

        at_first = H["worktree_fingerprint"](parent)
        subprocess.run(["git", "checkout", "-q", second], cwd=module, check=True)
        at_second = H["worktree_fingerprint"](parent)
        self.assertNotEqual(at_first, at_second)
        subprocess.run(["git", "checkout", "-q", third], cwd=module, check=True)
        at_third = H["worktree_fingerprint"](parent)
        self.assertNotEqual(at_second, at_third)
        (module / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        dirty = H["worktree_fingerprint"](parent)
        self.assertNotEqual(at_third, dirty)

    def test_recursive_protected_patterns_prune_git_and_runtime_boundaries(self) -> None:
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        harness = root / ".harness"
        runtime = harness / "runtime"
        runtime.mkdir(parents=True)
        state_file = harness / "state.json"
        state_file.write_text('{"revision": 1}\n', encoding="utf-8")
        (runtime / "probe").write_text("volatile\n", encoding="utf-8")
        (harness / "config.toml").write_text("stable = true\n", encoding="utf-8")

        for pattern in ("*", ".harness"):
            with self.subTest(pattern=pattern):
                cfg = {"protected_paths": [pattern]}
                baseline = H["protected_snapshot"](root, cfg)
                captured = set(baseline["files"])
                self.assertFalse(any(path == ".git" or path.startswith(".git/") for path in captured))
                self.assertNotIn(".harness/state.json", captured)
                self.assertFalse(any(
                    path == ".harness/runtime" or path.startswith(".harness/runtime/")
                    for path in captured
                ))
                state_file.write_text('{"revision": 2}\n', encoding="utf-8")
                (runtime / "probe").write_text("changed\n", encoding="utf-8")
                self.assertEqual(H["protected_snapshot"](root, cfg), baseline)
                state_file.write_text('{"revision": 1}\n', encoding="utf-8")
                (runtime / "probe").write_text("volatile\n", encoding="utf-8")

    def test_protected_path_validation_rejects_project_escape(self) -> None:
        for value in (
            "../secret", "nested/../../secret", "/absolute/path", "back\\slash",
            "*/marker.txt", "docs/*/nested.md",
        ):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                H["validate_project_relative_path"](
                    value, "protected path", allow_glob=True,
                )


class PackagingTests(unittest.TestCase):
    def test_skill_files_fit_limits_and_generated_bytecode_is_absent(self) -> None:
        for path in SKILL_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts or ".harness" in path.parts:
                continue
            self.assertLess(path.stat().st_size, 100_000, str(path))
            self.assertNotIn("__pycache__", path.parts)
            self.assertNotEqual(path.suffix, ".pyc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
