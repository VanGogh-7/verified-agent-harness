from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "harness"
loader = importlib.machinery.SourceFileLoader("lifecycle_harness", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
harness = importlib.util.module_from_spec(spec)
loader.exec_module(harness)


class LifecycleTests(unittest.TestCase):
    @contextlib.contextmanager
    def in_dir(self, path: pathlib.Path):
        old = pathlib.Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old)

    def git_project(self) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path]:
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Lifecycle Test"], cwd=root, check=True)
        (root / "README.md").write_text("# Existing project\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "Initial project"], cwd=root, check=True)
        return temp, root

    def write_reports(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        analyst = root / "analyst.json"
        analyst.write_text(json.dumps({
            "role": "analyst", "architecture": "A small existing application.",
            "protected_paths": ["src/api.py"], "quality_gates": [["python", "-m", "pytest"]],
            "context_files": ["README.md"], "adoption_constraints": ["No refactors"],
            "risks": []}), encoding="utf-8")
        auditor = root / "auditor.json"
        auditor.write_text(json.dumps({
            "role": "auditor", "verdict": "PASS", "findings": [],
            "architecture_disagreements": [], "gate_gaps": [],
            "protected_path_gaps": []}), encoding="utf-8")
        return analyst, auditor

    def test_empty_directory_greenfield_detection_is_read_only(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            before = root.stat().st_mtime_ns
            result = harness.lifecycle_detection(root)
            self.assertEqual(result["classification"], "EMPTY_DIRECTORY")
            self.assertEqual(result["mode"], "GREENFIELD_BOOTSTRAP")
            self.assertEqual(before, root.stat().st_mtime_ns)

    def test_existing_git_repository_without_harness(self):
        temp, root = self.git_project()
        try:
            result = harness.lifecycle_detection(root)
            self.assertEqual(result["classification"], "GIT_WITHOUT_HARNESS")
            self.assertEqual(result["mode"], "BROWNFIELD_ADOPTION")
            self.assertFalse(result["dirty_worktree"])
        finally:
            temp.cleanup()

    def test_compatible_harness_and_subdirectory_route_to_operation(self):
        temp, root = self.git_project()
        try:
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.init_project(argparse.Namespace())
            (root / "nested").mkdir()
            result = harness.lifecycle_detection(root / "nested")
            self.assertEqual(result["classification"], "COMPATIBLE_HARNESS")
            self.assertEqual(result["mode"], "HARNESS_OPERATION")
            self.assertEqual(result["project_root"], str(root))
        finally:
            temp.cleanup()

    def test_damaged_and_incompatible_harness(self):
        temp, root = self.git_project()
        try:
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.init_project(argparse.Namespace())
            (root / ".harness" / "state.json").unlink()
            self.assertEqual(harness.lifecycle_detection(root)["classification"], "DAMAGED_HARNESS")
            (root / ".harness" / "state.json").write_text(json.dumps(harness.initial_state()), encoding="utf-8")
            config = root / ".harness" / "config.toml"
            config.write_text(config.read_text(encoding="utf-8").replace(
                "config_version = 1", "config_version = 999"), encoding="utf-8")
            self.assertEqual(harness.lifecycle_detection(root)["classification"], "INCOMPATIBLE_HARNESS")
        finally:
            temp.cleanup()

    def test_dirty_worktree_is_reported_without_writes(self):
        temp, root = self.git_project()
        try:
            (root / "README.md").write_text("changed\n", encoding="utf-8")
            before = (root / "README.md").read_bytes()
            self.assertTrue(harness.lifecycle_detection(root)["dirty_worktree"])
            self.assertEqual(before, (root / "README.md").read_bytes())
        finally:
            temp.cleanup()

    def test_interrupted_adoption_recovery_is_detected(self):
        temp, root = self.git_project()
        try:
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.init_project(argparse.Namespace())
            harness.write_lifecycle_state(root, "ACTIVATION_INTERRUPTED", "BROWNFIELD_ADOPTION")
            result = harness.lifecycle_detection(root)
            self.assertEqual(result["lifecycle_state"], "ACTIVATION_INTERRUPTED")
            self.assertEqual(result["mode"], "HARNESS_OPERATION")
        finally:
            temp.cleanup()

    def test_missing_or_malformed_reports_stop_assessment(self):
        temp, root = self.git_project()
        try:
            analyst, auditor = self.write_reports(root)
            auditor.write_text('{"role":"auditor"}', encoding="utf-8")
            args = argparse.Namespace(analyst_report=str(analyst), auditor_report=str(auditor),
                                      json=True, operator_input="", operator_language="auto")
            with self.in_dir(root), self.assertRaises(harness.HarnessError):
                harness.assess_project(args)
            args.auditor_report = None
            with self.in_dir(root), self.assertRaises(harness.HarnessError):
                harness.assess_project(args)
        finally:
            temp.cleanup()

    def test_brownfield_dry_run_does_not_modify_business_code(self):
        temp, root = self.git_project()
        try:
            business = root / "service.py"
            business.write_text("VALUE = 1\n", encoding="utf-8")
            analyst, auditor = self.write_reports(root)
            report = root / "report.md"
            report.write_text("# Harness Adoption Report\n\nArchitecture and constraints are approved.\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
            args = argparse.Namespace(report=str(report), analyst_report=str(analyst),
                                      auditor_report=str(auditor), approved=True, dry_run=True,
                                      operator_input="", operator_language="auto")
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(args)
            self.assertFalse((root / ".harness").exists())
            self.assertEqual(before, {path: path.read_bytes() for path in root.iterdir() if path.is_file()})
        finally:
            temp.cleanup()

    def test_operator_language_is_conversation_scoped(self):
        self.assertEqual(harness.detect_operator_language("请继续实施"), "zh")
        self.assertEqual(harness.operator_message("assessment_ready", "zh"), "只读评估已完成，等待操作员审阅。")
        self.assertEqual(harness.detect_operator_language("Please continue"), "en")
        self.assertEqual(harness.operator_message("assessment_ready", "en"), "The read-only assessment is ready for operator review.")

    def test_operator_wording_is_not_corrected(self):
        wording = "Please dont changes API"
        self.assertEqual(harness.detect_operator_language(wording), "en")
        self.assertNotIn("don't change", harness.operator_message("approval_required", "en"))

    def test_repository_artifacts_and_codex_prompts_are_english(self):
        config = (ROOT / "skill" / "templates" / "project-config.toml").read_text(encoding="utf-8")
        implementer = (ROOT / "skill" / "templates" / "implementation-prompt.md").read_text(encoding="utf-8")
        reviewer = (ROOT / "skill" / "templates" / "review-prompt.md").read_text(encoding="utf-8")
        self.assertIn('agent_instruction_language = "en"', config)
        self.assertIn("Use English", implementer)
        self.assertIn("Use English", reviewer)

    def test_paths_identifiers_and_localization_boundary_remain_explicit(self):
        text = (ROOT / "lifecycle-skill" / "SKILL.md").read_text(encoding="utf-8")
        for token in ("paths", "commands", "Stage/Slice IDs", "JSON keys", "error codes"):
            self.assertIn(token, text)
        self.assertIn("Localized product strings", text)
        self.assertIn("localization keys", text)

    def test_hermes_configuration_script_backs_up_and_disables_automatic_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            config = root / "config.yaml"
            config.write_text("model: test\nmemory:\n  memory_enabled: true\ncurator:\n  enabled: true\n", encoding="utf-8")
            subprocess.run([str(ROOT / "scripts" / "configure-hermes")], check=True,
                           env={**os.environ, "HERMES_DEPLOY_HOME": str(root)},
                           stdout=subprocess.DEVNULL)
            value = yaml.safe_load(config.read_text(encoding="utf-8"))
            self.assertFalse(value["memory"]["memory_enabled"])
            self.assertFalse(value["memory"]["user_profile_enabled"])
            self.assertEqual(value["memory"]["nudge_interval"], 0)
            self.assertEqual(value["skills"]["creation_nudge_interval"], 0)
            self.assertTrue(value["skills"]["write_approval"])
            self.assertFalse(value["curator"]["enabled"])
            backups = list(root.glob("config.yaml.bak.lifecycle-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("memory_enabled: true", backups[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
