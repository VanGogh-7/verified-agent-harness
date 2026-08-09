from __future__ import annotations

import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy_transaction.py"
import importlib.util

deploy_spec = importlib.util.spec_from_file_location("deploy_transaction", DEPLOY)
deploy_module = importlib.util.module_from_spec(deploy_spec)
deploy_spec.loader.exec_module(deploy_module)
PRE_COMMIT_PHASES = deploy_module.PRE_COMMIT_PHASES


def snapshot(path: Path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return ("symlink", os.readlink(path), mode)
    if stat.S_ISREG(metadata.st_mode):
        return ("file", path.read_bytes(), mode)
    if stat.S_ISDIR(metadata.st_mode):
        return ("dir", mode, tuple(
            (child.name, snapshot(child)) for child in sorted(path.iterdir(), key=lambda item: item.name)
        ))
    return ("other", metadata.st_mode)


class DeploymentTests(unittest.TestCase):
    def test_plugin_config_migration_rejects_conflicting_activation(self):
        updated_config = runpy.run_path(str(ROOT / "scripts" / "configure-hermes"))["updated_config"]
        config = {
            "plugins": {
                "enabled": ["codex-harness-context"],
                "disabled": ["verified-agent-harness-context"],
                "entries": {},
            }
        }
        with self.assertRaisesRegex(ValueError, "both enabled and disabled"):
            updated_config(config)

    def test_plugin_registration_uses_verified_agent_identity(self):
        plugin_path = ROOT / "plugin" / "__init__.py"
        plugin_spec = importlib.util.spec_from_file_location("verified_agent_harness_plugin", plugin_path)
        assert plugin_spec is not None and plugin_spec.loader is not None
        plugin_module = importlib.util.module_from_spec(plugin_spec)
        plugin_spec.loader.exec_module(plugin_module)

        class Context:
            def __init__(self):
                self.calls = []

            def register_tool(self, **kwargs):
                self.calls.append(kwargs)

        context = Context()
        plugin_module.register(context)
        self.assertEqual(len(context.calls), 2)
        self.assertEqual({call["toolset"] for call in context.calls}, {"verified_agent_harness"})
        self.assertNotIn("Codex Harness", plugin_module.__doc__ or "")

    def fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="deployment-test-")
        base = Path(temp.name)
        payload = base / "payload"
        shutil.copytree(ROOT, payload, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", "*.tmp"))
        hermes = base / "hermes"
        bin_dir = base / "bin"
        group = base / "Group"
        for path in (hermes / "skills/software-development/codex-harness",
                     hermes / "skills/software-development/project-lifecycle-harness",
                     hermes / "plugins/codex-harness-context", bin_dir, group / ".harness"):
            path.mkdir(parents=True)
            (path / "old.txt").write_text(f"old:{path.name}\n", encoding="utf-8")
        (bin_dir / "harness").write_text("old launcher\n", encoding="utf-8")
        (group / ".harness/config.toml").write_text("old config\n", encoding="utf-8")
        (hermes / "config.yaml").write_text(
            "model: test\n"
            "memory:\n  memory_enabled: true\n"
            "skills:\n  creation_nudge_interval: 10\n"
            "plugins:\n"
            "  enabled: [codex-harness-context, observability/langfuse]\n"
            "  disabled: []\n"
            "  entries:\n"
            "    codex-harness-context:\n"
            "      allow_tool_override: false\n",
            encoding="utf-8")
        (hermes / "codex-harness-deployment-manifest.json").write_text("old manifest\n", encoding="utf-8")
        env = {**os.environ, "HERMES_DEPLOY_HOME": str(hermes),
               "HARNESS_BIN_DIR": str(bin_dir), "GROUP_PROJECT_ROOT": str(group)}
        targets = (
            hermes / "skills/software-development/codex-harness",
            hermes / "skills/software-development/verified-agent-harness",
            hermes / "skills/software-development/project-lifecycle-harness",
            hermes / "plugins/codex-harness-context",
            hermes / "plugins/verified-agent-harness-context", bin_dir / "harness",
            group / ".harness/config.toml", hermes / "config.yaml",
            hermes / "codex-harness-deployment-manifest.json",
            hermes / "verified-agent-harness-deployment-manifest.json",
        )
        return temp, base, payload, hermes, bin_dir, group, env, targets

    def deploy(self, payload, env, *, fault=None, doctor=None):
        command = [sys.executable, str(DEPLOY), "--payload-root", str(payload),
                   "--frozen-commit", "test-frozen-commit",
                   "--doctor-command-json", json.dumps(doctor or [sys.executable, "-c", "raise SystemExit(0)"])]
        deployment_env = dict(env)
        if fault:
            deployment_env["HARNESS_DEPLOY_FAULT_AFTER"] = fault
        return subprocess.run(command, env=deployment_env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_fault_at_each_deployment_phase_restores_byte_identical_targets(self):
        for phase in PRE_COMMIT_PHASES:
            temp, _base, payload, hermes, _bin_dir, _group, env, targets = self.fixture()
            try:
                before = [snapshot(path) for path in targets]
                result = self.deploy(payload, env, fault=phase)
                self.assertNotEqual(result.returncode, 0, phase)
                self.assertEqual(before, [snapshot(path) for path in targets], phase)
                self.assertFalse((hermes / "deployment-backups").exists(), phase)
            finally:
                temp.cleanup()

    def test_fault_before_and_after_backup_rename_restores_all_targets(self):
        phases = (
            "backup_legacy_skill_before_rename",
            "backup_legacy_skill_rename_error_before_move",
            "backup_legacy_skill_rename_error_after_move",
            "backup_legacy_skill_after_rename",
            "backup_launcher_before_rename",
            "backup_launcher_rename_error_before_move",
            "backup_launcher_rename_error_after_move",
            "backup_launcher_after_rename",
        )
        for phase in phases:
            temp, _base, payload, hermes, _bin_dir, _group, env, targets = self.fixture()
            try:
                before = [snapshot(path) for path in targets]
                result = self.deploy(payload, env, fault=phase)
                self.assertNotEqual(result.returncode, 0, phase)
                self.assertEqual(before, [snapshot(path) for path in targets], phase)
                self.assertFalse((hermes / "deployment-backups").exists(), phase)
            finally:
                temp.cleanup()

    def test_existing_deployment_target_symlink_is_rejected_before_mutation(self):
        temp, base, payload, hermes, bin_dir, _group, env, targets = self.fixture()
        try:
            launcher = bin_dir / "harness"
            launcher.unlink()
            referent = base / "outside-launcher"
            referent.write_bytes(b"outside launcher\n")
            launcher.symlink_to(referent)
            before = [snapshot(path) for path in targets]

            result = self.deploy(payload, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deployment target is symlinked", result.stderr)
            self.assertEqual(before, [snapshot(path) for path in targets])
            self.assertEqual(referent.read_bytes(), b"outside launcher\n")
            self.assertFalse((hermes / "deployment-backups").exists())
        finally:
            temp.cleanup()

    def test_cleanup_failure_after_commit_retains_archive_and_installed_targets(self):
        temp, _base, payload, hermes, _bin_dir, _group, env, targets = self.fixture()
        try:
            before = [snapshot(path) for path in targets]
            result = self.deploy(payload, env, fault="temporary_backup_cleanup")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(before, [snapshot(path) for path in targets])
            archives = list((hermes / "deployment-backups").iterdir())
            self.assertEqual(len(archives), 1)
            self.assertIn("temporary backup cleanup incomplete", result.stderr)
        finally:
            temp.cleanup()

    def test_retained_archive_preserves_nested_symlink_identity(self):
        temp, base, payload, hermes, _bin_dir, _group, env, _targets = self.fixture()
        try:
            old_skill = hermes / "skills/software-development/codex-harness"
            referent = base / "outside-old-skill"
            referent.write_text("outside bytes\n", encoding="utf-8")
            (old_skill / "nested-link").symlink_to(referent)
            expected = snapshot(old_skill)

            result = self.deploy(payload, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            archives = list((hermes / "deployment-backups").iterdir())
            self.assertEqual(len(archives), 1)
            self.assertEqual(snapshot(archives[0] / "legacy_skill"), expected)
            self.assertEqual(referent.read_text(encoding="utf-8"), "outside bytes\n")
        finally:
            temp.cleanup()

    def test_rollback_preserves_unrecognized_target_and_backup(self):
        temp, base, payload, hermes, bin_dir, _group, env, _targets = self.fixture()
        try:
            launcher = bin_dir / "harness"
            original = launcher.read_bytes()
            moved = base / "transaction-launcher"
            sentinel = b"unrelated concurrent launcher\n"
            doctor = [
                sys.executable,
                "-c",
                (
                    "import os, pathlib; "
                    f"p=pathlib.Path({str(launcher)!r}); "
                    f"os.replace(p, pathlib.Path({str(moved)!r})); "
                    f"p.write_bytes({sentinel!r}); "
                    "raise SystemExit(1)"
                ),
            ]

            result = self.deploy(payload, env, doctor=doctor)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized target preserved", result.stderr)
            self.assertEqual(launcher.read_bytes(), sentinel)
            backups = list(bin_dir.glob(".harness.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
        finally:
            temp.cleanup()

    def test_rollback_preserves_distinct_same_content_replacement_and_backup(self):
        temp, base, payload, _hermes, bin_dir, _group, env, _targets = self.fixture()
        try:
            launcher = bin_dir / "harness"
            original = launcher.read_bytes()
            replacement = (payload / "bin/harness").read_bytes()
            moved = base / "transaction-launcher"
            doctor = [
                sys.executable,
                "-c",
                (
                    "import os, pathlib, stat; "
                    f"p=pathlib.Path({str(launcher)!r}); "
                    "data=p.read_bytes(); mode=stat.S_IMODE(p.stat().st_mode); "
                    f"os.replace(p, pathlib.Path({str(moved)!r})); "
                    "p.write_bytes(data); os.chmod(p, mode); "
                    "raise SystemExit(1)"
                ),
            ]

            result = self.deploy(payload, env, doctor=doctor)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized target preserved", result.stderr)
            self.assertEqual(launcher.read_bytes(), replacement)
            backups = list(bin_dir.glob(".harness.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
        finally:
            temp.cleanup()

    def test_quarantine_detects_replacement_between_token_check_and_rename(self):
        with tempfile.TemporaryDirectory(prefix="deployment-quarantine-") as raw:
            base = Path(raw)
            target = base / "target"
            quarantine = base / "quarantine"
            moved = base / "transaction-object"
            target.write_bytes(b"same payload\n")
            expected = deploy_module.object_token(target)
            real_replace = os.replace

            def race(source, destination):
                if Path(source) == target:
                    real_replace(target, moved)
                    target.write_bytes(b"same payload\n")
                return real_replace(source, destination)

            with mock.patch.object(deploy_module.os, "replace", side_effect=race):
                status = deploy_module.quarantine_owned_target(target, expected, quarantine)

            self.assertEqual(status, "unrecognized_quarantined")
            self.assertFalse(target.exists())
            self.assertEqual(quarantine.read_bytes(), b"same payload\n")
            self.assertEqual(moved.read_bytes(), b"same payload\n")

    def test_rename_noreplace_preserves_concurrent_destination(self):
        with tempfile.TemporaryDirectory(prefix="deployment-restore-") as raw:
            base = Path(raw)
            backup = base / "backup"
            target = base / "target"
            backup.write_bytes(b"verified prior object\n")
            target.write_bytes(b"concurrent external object\n")

            with self.assertRaises(OSError) as raised:
                deploy_module.rename_noreplace(backup, target)

            self.assertEqual(raised.exception.errno, deploy_module.errno.EEXIST)
            self.assertEqual(backup.read_bytes(), b"verified prior object\n")
            self.assertEqual(target.read_bytes(), b"concurrent external object\n")

    def test_successful_rollback_retains_owned_quarantine_for_prior_target(self):
        temp, _base, payload, _hermes, bin_dir, _group, env, _targets = self.fixture()
        try:
            launcher = bin_dir / "harness"
            original = launcher.read_bytes()
            staged = (payload / "bin/harness").read_bytes()
            result = self.deploy(payload, env, doctor=[sys.executable, "-c", "raise SystemExit(1)"])

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(launcher.read_bytes(), original)
            quarantines = list(bin_dir.glob(".harness.rollback-*"))
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), staged)
            self.assertIn(str(quarantines[0]), result.stderr)
        finally:
            temp.cleanup()

    def test_successful_rollback_retains_owned_quarantine_for_absent_target(self):
        temp, _base, payload, hermes, _bin_dir, _group, env, _targets = self.fixture()
        try:
            manifest = hermes / "verified-agent-harness-deployment-manifest.json"
            manifest.unlink(missing_ok=True)
            staged = json.dumps(
                deploy_module.payload_manifest(payload, "test-frozen-commit"),
                indent=2, sort_keys=True,
            ).encode("utf-8") + b"\n"
            result = self.deploy(payload, env, doctor=[sys.executable, "-c", "raise SystemExit(1)"])

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(manifest.exists())
            quarantines = list(hermes.glob(".verified-agent-harness-deployment-manifest.json.rollback-*"))
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), staged)
            self.assertIn(str(quarantines[0]), result.stderr)
        finally:
            temp.cleanup()

    def test_absent_target_rollback_removes_in_place_mutated_staged_object(self):
        temp, _base, payload, hermes, _bin_dir, _group, env, _targets = self.fixture()
        try:
            manifest = hermes / "verified-agent-harness-deployment-manifest.json"
            manifest.unlink(missing_ok=True)
            doctor = [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(manifest)!r}).write_text('mutated in place') ; raise SystemExit(1)",
            ]

            result = self.deploy(payload, env, doctor=doctor)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(manifest.exists(), result.stderr)
        finally:
            temp.cleanup()

    def test_absent_target_rollback_preserves_atomically_replaced_unrelated_object(self):
        temp, base, payload, hermes, _bin_dir, _group, env, _targets = self.fixture()
        try:
            manifest = hermes / "verified-agent-harness-deployment-manifest.json"
            manifest.unlink(missing_ok=True)
            replacement = base / "unrelated-manifest"
            sentinel = b"unrelated concurrent manifest\n"
            replacement.write_bytes(sentinel)
            doctor = [
                sys.executable,
                "-c",
                f"import os; os.replace({str(replacement)!r}, {str(manifest)!r}); raise SystemExit(1)",
            ]

            result = self.deploy(payload, env, doctor=doctor)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized target preserved", result.stderr)
            self.assertEqual(manifest.read_bytes(), sentinel)
        finally:
            temp.cleanup()

    def test_restore_rename_faults_reconcile_before_and_after_move(self):
        for phase in ("restore_launcher_before_move", "restore_launcher_after_move"):
            temp, _base, payload, _hermes, bin_dir, _group, env, targets = self.fixture()
            try:
                before = [snapshot(path) for path in targets]
                result = self.deploy(
                    payload,
                    env,
                    fault=phase,
                    doctor=[sys.executable, "-c", "raise SystemExit(1)"],
                )
                self.assertNotEqual(result.returncode, 0, phase)
                if phase.endswith("after_move"):
                    self.assertEqual(before, [snapshot(path) for path in targets], phase)
                    self.assertFalse(list(bin_dir.glob(".harness.backup-*")), phase)
                else:
                    self.assertIsNone(snapshot(bin_dir / "harness"), phase)
                    backups = list(bin_dir.glob(".harness.backup-*"))
                    self.assertEqual(len(backups), 1, phase)
                    self.assertEqual(snapshot(backups[0]), before[5], phase)
            finally:
                temp.cleanup()

    def test_successful_temporary_deployment_excludes_generated_files_and_records_manifest(self):
        temp, _base, payload, hermes, bin_dir, group, env, _targets = self.fixture()
        try:
            result = self.deploy(payload, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = hermes / "skills/software-development/project-lifecycle-harness"
            self.assertTrue((installed / "schemas/final-review.schema.json").is_file())
            self.assertTrue((installed / "scripts/harness").is_file())
            stage = hermes / "skills/software-development/verified-agent-harness"
            self.assertTrue((stage / "scripts/harness_core.py").is_file())
            self.assertTrue((stage / "scripts/harness_commands.py").is_file())
            self.assertTrue((stage / "scripts/test_harness.py").is_file())
            self.assertTrue((stage / "references/contracts/verification.schema.json").is_file())
            self.assertFalse(any(path.name == "__pycache__" or path.suffix == ".pyc"
                                 for path in hermes.rglob("*")))
            self.assertEqual((bin_dir / "harness").read_bytes(), (payload / "bin/harness").read_bytes())
            self.assertEqual((group / ".harness/config.toml").read_bytes(),
                             (payload / "projects/Group/config.toml").read_bytes())
            configured = yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8"))
            self.assertIn("verified-agent-harness-context", configured["plugins"]["enabled"])
            self.assertNotIn("codex-harness-context", configured["plugins"]["enabled"])
            self.assertEqual(configured["plugins"]["entries"]["verified-agent-harness-context"],
                             {"allow_tool_override": False})
            self.assertNotIn("codex-harness-context", configured["plugins"]["entries"])
            manifest = hermes / "verified-agent-harness-deployment-manifest.json"
            self.assertIn("deployment_manifest_sha256=", result.stdout)
            self.assertEqual(json.loads(manifest.read_text())["frozen_commit"], "test-frozen-commit")
            self.assertEqual(len(list((hermes / "deployment-backups").iterdir())), 1)
        finally:
            temp.cleanup()

    def test_deployment_requires_advisory_contract_and_installed_routing_is_exact(self):
        temp, _base, payload, hermes, _bin_dir, _group, env, _targets = self.fixture()
        try:
            contract = payload / "skill/references/contracts/advisory.schema.json"
            saved_contract = contract.read_bytes()
            contract.unlink()
            rejected = self.deploy(payload, env)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("advisory.schema.json", rejected.stderr)
            contract.write_bytes(saved_contract)

            prompt = payload / "skill/templates/advisory-prompt.md"
            saved_prompt = prompt.read_bytes()
            prompt.unlink()
            rejected = self.deploy(payload, env)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("advisory-prompt.md", rejected.stderr)
            prompt.write_bytes(saved_prompt)

            result = self.deploy(payload, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = hermes / "skills/software-development/verified-agent-harness"
            self.assertTrue((installed / "references/contracts/advisory.schema.json").is_file())
            installed_config = tomllib.loads(
                (payload / "projects/Group/config.toml").read_text(encoding="utf-8")
            )
            spec = importlib.util.spec_from_file_location(
                "installed_codex_cli_reference", installed / "adapters/codex_cli.py"
            )
            assert spec is not None and spec.loader is not None
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            expected = {
                "Implementer": ("implementer", "workspace-write"),
                "Correctness Reviewer": ("reviewer", "read-only"),
                "Tester": ("tester", "read-only"),
                "Security Reviewer": ("security_reviewer", "read-only"),
                "Verifier": ("verifier", "read-only"),
                "Explorer": ("explorer", "read-only"),
                "Researcher": ("researcher", "read-only"),
                "Test Triage": ("test_triage", "read-only"),
                "Log Triage": ("log_triage", "read-only"),
                "Architecture Analyst": ("architecture_analyst", "read-only"),
                "Independent Auditor": ("auditor", "read-only"),
                "Final Lifecycle Reviewer": ("final_lifecycle_reviewer", "read-only"),
            }
            with tempfile.TemporaryDirectory() as raw:
                workdir = Path(raw)
                prompt = workdir / "prompt.md"
                schema = workdir / "schema.json"
                output = workdir / "output.json"
                prompt.write_text("prompt\n", encoding="utf-8")
                schema.write_text("{}\n", encoding="utf-8")
                for label, (key, access) in expected.items():
                    model = installed_config["agent_runtime"]["models"][key]
                    effort = installed_config["agent_runtime"]["reasoning_efforts"][key]
                    with self.subTest(role=label), mock.patch.object(adapter.subprocess, "run") as run:
                        adapter.sys.argv = [
                            "codex_cli.py", "--role", label, "--access", access,
                            "--workdir", str(workdir), "--prompt", str(prompt),
                            "--schema", str(schema), "--output", str(output),
                            "--model-alias", model, "--reasoning-effort", effort,
                            "--ephemeral", "true",
                        ]
                        run.return_value = types.SimpleNamespace(returncode=0)
                        self.assertEqual(adapter.main(), 0)
                        self.assertFalse(run.call_args.kwargs.get("shell", False))
                        self.assertEqual(run.call_args.args[0], [
                            "codex", "exec", "--sandbox", access, "--ephemeral",
                            "--model", model, "-c", f'model_reasoning_effort="{effort}"',
                            "--output-schema", str(schema), "--output-last-message", str(output),
                            "--color", "never", "-C", str(workdir), "-",
                        ])
        finally:
            temp.cleanup()

    def test_doctor_payload_mutation_is_detected_and_rolled_back(self):
        temp, _base, payload, hermes, _bin_dir, _group, env, targets = self.fixture()
        try:
            before = [snapshot(path) for path in targets]
            installed_skill = hermes / "skills/software-development/verified-agent-harness"
            doctor = [sys.executable, "-c",
                      f"from pathlib import Path; Path({str(installed_skill / '__pycache__/bad.pyc')!r}).parent.mkdir(); "
                      f"Path({str(installed_skill / '__pycache__/bad.pyc')!r}).write_bytes(b'generated')"]
            result = self.deploy(payload, env, doctor=doctor)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed during post-deploy doctor", result.stderr)
            self.assertEqual(before, [snapshot(path) for path in targets])
        finally:
            temp.cleanup()

    def test_installed_launcher_resolves_sibling_schemas_for_assess_adopt_and_activate(self):
        temp, base, payload, hermes, bin_dir, _group, env, _targets = self.fixture()
        try:
            result = self.deploy(payload, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = bin_dir / "harness"
            run_env = {**env, "HERMES_HOME": str(hermes)}
            version = subprocess.run([str(launcher), "--version"], cwd=base, env=run_env,
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(version.returncode, 0, version.stdout + version.stderr)
            self.assertEqual(version.stdout.strip(), "harness 1.0.0")
            project = base / "InstalledProject"
            project.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Installed Test"], cwd=project, check=True)
            (project / "README.md").write_text(
                "# Installed Project\n\nEnglish architecture context for installed testing.\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "Initial project"], cwd=project, check=True)
            analyst = {
                "role": "analyst", "verdict": "PASS", "architecture": "Installed test project.",
                "protected_paths": ["README.md"], "quality_gates": [["python", "-m", "pytest", "-q"]],
                "context_files": ["README.md"], "adoption_constraints": ["No product changes"],
                "findings": [], "blockers": [], "gate_gaps": [], "protected_path_gaps": [],
                "unresolved_risks": [], "recommendations": [],
            }
            auditor = {"role": "auditor", "verdict": "PASS", "findings": [], "blockers": [],
                       "architecture_disagreements": [], "gate_gaps": [], "protected_path_gaps": [],
                       "unresolved_risks": [], "language_violations": [], "recommendations": []}
            final = {"role": "final_reviewer", "verdict": "PASS", "findings": [], "blockers": [],
                     "architecture_disagreements": [], "gate_gaps": [], "protected_path_gaps": [],
                     "unresolved_risks": [], "language_violations": [],
                     "recommendations": [], "language_compliance": "PASS"}
            for name, value in (("analyst.json", analyst), ("auditor.json", auditor), ("final.json", final)):
                (project / name).write_text(json.dumps(value), encoding="utf-8")
            report = project / "adoption-source.md"
            report.write_text(
                "# Harness Adoption Report\n\nEnglish architecture and validation constraints are approved.\n\n"
                "<!-- harness-adoption-approval:start -->\n<APPROVAL_MANIFEST>\n"
                "<!-- harness-adoption-approval:end -->\n", encoding="utf-8")
            assess = subprocess.run([str(launcher), "assess", "--analyst-report", "analyst.json",
                                     "--auditor-report", "auditor.json", "--json"],
                                    cwd=project, env=run_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(assess.returncode, 0, assess.stdout + assess.stderr)
            dry = subprocess.run([str(launcher), "adopt", "--report", "adoption-source.md",
                                  "--analyst-report", "analyst.json", "--auditor-report", "auditor.json",
                                  "--dry-run"], cwd=project, env=run_env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            plan_hash = re.search(r'plan_sha256="([0-9a-f]{64})"', dry.stdout).group(1)
            approval_block = json.loads(dry.stdout.split(" approval_block=", 1)[1].strip())
            text = report.read_text(encoding="utf-8")
            text = re.sub(r"<!-- harness-adoption-approval:start -->.*?<!-- harness-adoption-approval:end -->",
                          approval_block, text, flags=re.DOTALL)
            report.write_text(text, encoding="utf-8")
            adopt = subprocess.run([str(launcher), "adopt", "--report", "adoption-source.md",
                                    "--analyst-report", "analyst.json", "--auditor-report", "auditor.json",
                                    "--approved", "--approved-plan-hash", plan_hash], cwd=project, env=run_env,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(adopt.returncode, 0, adopt.stdout + adopt.stderr)
            git_dir = Path(subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"],
                                                   cwd=project, text=True).strip())
            for path in (project / ".harness/state.json", git_dir / "harness-control/state.json"):
                state = json.loads(path.read_text())
                state["workflow_state"] = "STAGE_COMPLETED"
                path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            activate = subprocess.run([str(launcher), "activate", "--final-review", "final.json"],
                                      cwd=project, env=run_env, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(activate.returncode, 0, activate.stdout + activate.stderr)
            self.assertEqual(json.loads((project / ".harness/lifecycle.json").read_text())["lifecycle_state"],
                             "HARNESS_READY")
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
