from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import multiprocessing
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

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
        temp = tempfile.TemporaryDirectory(prefix="lifecycle-project-")
        root = pathlib.Path(temp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Lifecycle Test"], cwd=root, check=True)
        (root / "README.md").write_text("# Existing Project\n\nEnglish architecture context for adoption.\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "Initial project"], cwd=root, check=True)
        return temp, root

    def report_values(self):
        analyst = {
            "role": "analyst", "verdict": "PASS",
            "architecture": "A small existing application with a documented entry point.",
            "protected_paths": ["README.md"],
            "quality_gates": [["python", "-m", "pytest", "-q"]],
            "context_files": ["README.md"], "adoption_constraints": ["No refactors"],
            "findings": [], "blockers": [], "gate_gaps": [], "protected_path_gaps": [],
            "unresolved_risks": [], "recommendations": ["Add more tests later"],
        }
        auditor = {
            "role": "auditor", "verdict": "PASS", "findings": [], "blockers": [],
            "architecture_disagreements": [], "gate_gaps": [], "protected_path_gaps": [],
            "unresolved_risks": [], "language_violations": [], "recommendations": [],
        }
        final = {
            "role": "final_reviewer", "verdict": "PASS", "findings": [], "blockers": [],
            "architecture_disagreements": [], "gate_gaps": [], "protected_path_gaps": [],
            "unresolved_risks": [], "language_violations": [],
            "recommendations": [], "language_compliance": "PASS",
        }
        return analyst, auditor, final

    def write_reports(self, root: pathlib.Path):
        analyst_value, auditor_value, final_value = self.report_values()
        analyst = root / "analyst.json"
        auditor = root / "auditor.json"
        final = root / "final.json"
        report = root / "adoption-source.md"
        analyst.write_text(json.dumps(analyst_value), encoding="utf-8")
        auditor.write_text(json.dumps(auditor_value), encoding="utf-8")
        final.write_text(json.dumps(final_value), encoding="utf-8")
        report.write_text(
            "# Harness Adoption Report\n\nThe architecture, protected paths, context files, "
            "derived quality gates, and adoption constraints are approved.\n\n"
            "<!-- harness-adoption-approval:start -->\n<APPROVAL_MANIFEST>\n"
            "<!-- harness-adoption-approval:end -->\n", encoding="utf-8")
        return report, analyst, auditor, final

    def adoption_args(self, report, analyst, auditor, **overrides):
        values = dict(report=str(report), analyst_report=str(analyst), auditor_report=str(auditor),
                      approved=False, approved_plan_hash=None, dry_run=False, resume=False, rollback=False,
                      operator_input="", operator_language="auto")
        values.update(overrides)
        return argparse.Namespace(**values)

    def approved_plan(self, root, report, analyst_path, auditor_path):
        args = self.adoption_args(report, analyst_path, auditor_path)
        text, analyst, _auditor, hashes = harness.validated_adoption_inputs(root, args)
        plan = harness.build_adoption_plan(root, text, analyst, hashes)
        report.write_text(harness.inject_adoption_approval(text, plan["approval_block"]), encoding="utf-8")
        text, analyst, _auditor, hashes = harness.validated_adoption_inputs(root, args)
        final_plan = harness.build_adoption_plan(root, text, analyst, hashes)
        self.assertEqual(plan["plan_sha256"], final_plan["plan_sha256"])
        return final_plan

    def test_empty_and_existing_repository_detection_is_read_only(self):
        with tempfile.TemporaryDirectory(prefix="empty-project-") as raw:
            root = pathlib.Path(raw)
            before = root.stat().st_mtime_ns
            self.assertEqual(harness.lifecycle_detection(root)["classification"], "EMPTY_DIRECTORY")
            self.assertEqual(before, root.stat().st_mtime_ns)
        temp, root = self.git_project()
        try:
            result = harness.lifecycle_detection(root)
            self.assertEqual(result["classification"], "GIT_WITHOUT_HARNESS")
            self.assertFalse(result["dirty_worktree"])
        finally:
            temp.cleanup()

    def test_compatible_damaged_incompatible_and_dirty_detection(self):
        temp, root = self.git_project()
        try:
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.init_project(argparse.Namespace())
            self.assertEqual(harness.lifecycle_detection(root)["classification"], "COMPATIBLE_HARNESS")
            (root / ".harness/state.json").unlink()
            self.assertEqual(harness.lifecycle_detection(root)["classification"], "DAMAGED_HARNESS")
            (root / ".harness/state.json").write_text(json.dumps(harness.initial_state()), encoding="utf-8")
            config = root / ".harness/config.toml"
            config.write_text(config.read_text().replace("config_version = 1", "config_version = 999"), encoding="utf-8")
            self.assertEqual(harness.lifecycle_detection(root)["classification"], "INCOMPATIBLE_HARNESS")
            self.assertTrue(harness.lifecycle_detection(root)["dirty_worktree"])
        finally:
            temp.cleanup()

    def test_assessment_with_valid_reports_is_byte_and_mtime_read_only(self):
        temp, root = self.git_project()
        try:
            _report, analyst, auditor, _final = self.write_reports(root)
            watched = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                       for path in root.iterdir() if path.is_file()}
            args = argparse.Namespace(analyst_report=str(analyst), auditor_report=str(auditor),
                                      json=True, operator_input="", operator_language="auto")
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.assess_project(args)
            self.assertEqual(watched, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                       for path in root.iterdir() if path.is_file()})
        finally:
            temp.cleanup()

    def test_contradictory_pass_reports_fail_closed(self):
        _analyst, _auditor, _final = self.report_values()
        for role, base in zip(("analyst", "auditor", "final_reviewer"), self.report_values()):
            for field in harness.REPORT_EMPTY_ON_PASS:
                value = json.loads(json.dumps(base))
                value[field] = [f"critical {field}"]
                with tempfile.TemporaryDirectory() as raw:
                    path = pathlib.Path(raw) / "report.json"
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.subTest(role=role, field=field), self.assertRaises(harness.HarnessError):
                        harness.lifecycle_report(path, role)
        final = self.report_values()[2]
        final["language_compliance"] = "FAIL"
        with tempfile.TemporaryDirectory() as raw:
            path = pathlib.Path(raw) / "final.json"
            path.write_text(json.dumps(final), encoding="utf-8")
            self.assertEqual(harness.lifecycle_report(path, "final_reviewer")["language_compliance"], "FAIL")

    def test_untrusted_shell_and_arbitrary_commands_are_rejected(self):
        temp, root = self.git_project()
        try:
            unsafe = (["bash", "-c", "make test"], ["sh", "-c", "test"],
                      ["python", "-c", "print(1)"], ["/tmp/tool", "test"],
                      ["npm", "run", "$(touch bad)"], ["npm", "run", "arbitrary"])
            for argv in unsafe:
                with self.subTest(argv=argv), self.assertRaises(harness.HarnessError):
                    harness.validate_gate_argv(root, list(argv))
        finally:
            temp.cleanup()

    def test_context_paths_reject_escape_control_symlink_fifo_and_socket(self):
        temp, root = self.git_project()
        sock = None
        try:
            (root / ".harness").mkdir()
            outside = pathlib.Path(temp.name).parent / "outside-context"
            outside.write_text("outside", encoding="utf-8")
            (root / "linked").symlink_to(outside)
            os.mkfifo(root / "fifo")
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(root / "socket"))
            for value in ("../outside-context", ".git/config", ".harness/state.json",
                          "linked", "fifo", "socket"):
                with self.subTest(value=value), self.assertRaises(harness.HarnessError):
                    harness.safe_project_file(root, value, "context file")
            outside.unlink(missing_ok=True)
        finally:
            if sock is not None:
                sock.close()
            temp.cleanup()

    def test_language_lint_and_engineering_names(self):
        english = "This engineering document explains architecture, validation, security, and deployment boundaries."
        chinese = "这是一个完全使用中文编写的工程架构说明，其中包含大量非英文技术叙述和操作要求。"
        localized = (english + "\n<!-- language-lint:allow-localized -->\n你好，用户！\n"
                     "<!-- /language-lint -->\n")
        self.assertEqual(harness.language_lint(english)["classification"], "PASS")
        self.assertEqual(harness.language_lint(chinese)["classification"], "FAIL")
        self.assertEqual(harness.language_lint(localized)["classification"], "WARN")
        with self.assertRaises(harness.HarnessError):
            harness.require_strict_english(localized, "engineering document", markdown=True)
        for value in ("阶段一", "Stage_一", "module/模块.py"):
            with self.assertRaises(harness.HarnessError):
                harness.validate_engineering_name(value, "identifier", allow_path="/" in value)
        with self.assertRaises(harness.HarnessError):
            harness.validate_machine_language({"bad-key": "HARNESS_READY"})
        with self.assertRaises(harness.HarnessError):
            harness.validate_machine_language({"lifecycle_state": "未知状态"})

    def test_dry_run_reports_rejected_symlink_target(self):
        with tempfile.TemporaryDirectory(prefix="BootstrapProject-") as raw:
            root = pathlib.Path(raw)
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("external data\n", encoding="utf-8")
            (root / ".gitignore").symlink_to(outside)
            try:
                brief = ("# Project Brief\n\nBuild a secure service for test users with "
                         "documented deployment and deterministic acceptance criteria.\n")
                plan = harness.build_bootstrap_plan(root, brief)
                entry = next(item for item in plan["operations"] if item["path"] == ".gitignore")
                self.assertEqual(entry["action"], "REJECT")
                self.assertEqual(outside.read_text(encoding="utf-8"), "external data\n")
            finally:
                outside.unlink(missing_ok=True)

    def test_operator_language_is_conversation_scoped_without_grammar_correction(self):
        self.assertEqual(harness.detect_operator_language("请继续实施"), "zh")
        self.assertEqual(harness.operator_message("assessment_ready", "zh"), "只读评估已完成，等待操作员审阅。")
        self.assertEqual(harness.detect_operator_language("Please dont changes API"), "en")
        self.assertNotIn("don't change", harness.operator_message("approval_required", "en"))

    def test_strict_language_is_per_field_per_section_with_configured_localization_only(self):
        analyst, _auditor, _final = self.report_values()
        analyst["adoption_constraints"] = ["Keep public APIs stable.", "不得修改公开接口"]
        with tempfile.TemporaryDirectory() as raw:
            path = pathlib.Path(raw) / "analyst.json"
            path.write_text(json.dumps(analyst), encoding="utf-8")
            with self.assertRaises(harness.HarnessError):
                harness.lifecycle_report(path, "analyst")
            adoption = pathlib.Path(raw) / "HARNESS_ADOPTION_REPORT.md"
            adoption.write_text("# Architecture\n\nEnglish architecture is approved.\n\n## Constraint\n\n不得升级依赖。\n",
                                encoding="utf-8")
            with self.assertRaises(harness.HarnessError):
                harness.ensure_english_artifact(adoption, "HARNESS_ADOPTION_REPORT")
        self.assertEqual(harness.engineering_language_check(
            "locales/zh.json", '{"welcome": "欢迎"}', ["locales/"]), "PASS")
        with self.assertRaises(harness.HarnessError):
            harness.engineering_language_check("locales/zh.json", '{"欢迎": "Welcome"}', ["locales/"])
        with self.assertRaises(harness.HarnessError):
            harness.engineering_language_check("docs/design.md", "<!-- language-lint:allow-localized -->\n中文约束\n<!-- /language-lint -->")
        with self.assertRaises(harness.HarnessError):
            harness.engineering_language_check("locales/zh.md", "中文产品文案", ["locales/"])

    def test_markdown_headings_are_independent_strict_engineering_units(self):
        rejected = {
            "non-English top-level heading": "# 系统架构\n\nThe service uses a stable boundary.\n",
            "non-English nested heading": "# Architecture\n\nEnglish body text.\n\n### 安全边界\n",
            "mixed-language heading": "## Deployment 部署流程\n\nUse the approved release process.\n",
            "attempted heading suppression": (
                "<!-- language-lint:allow-localized -->\n## 不得检查此标题\n<!-- /language-lint -->\n"
            ),
        }
        for label, document in rejected.items():
            with self.subTest(label=label), self.assertRaises(harness.HarnessError):
                harness.engineering_language_check("docs/design.md", document)

        accepted = {
            "all ATX levels": "\n".join(f"{'#' * level} English Heading" for level in range(1, 7)),
            "code identifiers": "# Configure `rollback_adoption` for Harness API\n",
            "path and command": "## Run `./scripts/verify full` for `docs/design.md`\n",
            "approved proper nouns": "# Hermes and Codex Harness Architecture\n",
        }
        for label, document in accepted.items():
            with self.subTest(label=label):
                self.assertEqual(harness.engineering_language_check("docs/design.md", document), "PASS")

    def test_operator_chinese_does_not_localize_generated_engineering_headings(self):
        self.assertEqual(harness.operator_message("harness_ready", "zh"), "项目已进入 HARNESS_READY。")
        with self.assertRaises(harness.HarnessError):
            harness.engineering_language_check("HARNESS_ADOPTION_REPORT.md", "# 采用报告\n\nEnglish body.\n")

    def test_markdown_heading_entities_are_decoded_before_language_lint(self):
        rejected = {
            "decimal": "# &#20013;&#25991;\n",
            "hexadecimal": "## &#x4E2D;&#x6587;\n",
            "mixed literal and encoded": "# Architecture 中文 &#x5B89;&#x5168;\n",
            "double encoded": "# &amp;#20013;&amp;#25991;\n",
            "encoded nested heading": "# Architecture\n\n### &#20013;&#25991; constraint\n",
            "named reference": "# &zhcy;&icy;&rcy; constraint\n",
        }
        for label, document in rejected.items():
            with self.subTest(label=label), self.assertRaises(harness.HarnessError):
                harness.engineering_language_check("docs/design.md", document)
        accepted = {
            "structural exclusions": (
                "# Run `./scripts/verify full` for `RollbackAction` in `skill/scripts/harness`\n"
            ),
            "encoded code exclusion": "# Configure `&#20013;&#25991;_identifier` safely\n",
            "malformed numeric reference": "# Deployment &#xZZ; validation\n",
            "malformed named reference": "# Deployment &not-a-real-entity; validation\n",
        }
        for label, document in accepted.items():
            with self.subTest(label=label):
                self.assertEqual(harness.engineering_language_check("docs/design.md", document), "PASS")
        self.assertEqual(harness.engineering_language_check(
            "locales/zh.json", '{"title": "&#20013;&#25991;"}', ["locales/"]), "PASS")
        with self.assertRaises(harness.HarnessError):
            harness.engineering_language_check(
                "docs/localized.md", "# &#20013;&#25991;\n", ["locales/"])

    def test_decoded_markdown_delimiters_never_create_structural_exclusions(self):
        rejected = {
            "decimal encoded backticks": "# &#96;中文&#96;\n",
            "hex encoded backticks": "# &#x60;中文&#x60;\n",
            "named encoded comment": "# &lt;!-- 中文 --&gt;\n",
            "mixed English and encoded code": "# Validate &#96;中文&#96; safely\n",
            "double encoded backticks": "# &amp;#96;中文&amp;#96;\n",
            "double encoded comment": "# &amp;lt;!-- 中文 --&amp;gt;\n",
            "malformed delimiter entity": "# Validate &#96中文&#96 safely\n",
            "placeholder-like source": "# \u0000HXL0\u0000 中文\n",
        }
        for label, document in rejected.items():
            with self.subTest(label=label), self.assertRaises(harness.HarnessError):
                harness.engineering_language_check("docs/design.md", document)

        accepted = {
            "genuine code span": "## `ToolCallingAgent::run()` Compatibility\n",
            "genuine source comment": "# Validate <!-- 中文 internal note --> safely\n",
        }
        for label, document in accepted.items():
            with self.subTest(label=label):
                self.assertEqual(
                    harness.engineering_language_check("docs/design.md", document), "PASS")

    def test_mixed_language_stage_plan_is_rejected(self):
        temp, root = self.git_project()
        try:
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.init_project(argparse.Namespace())
            plan = root / "stage-plan.md"
            plan.write_text("# Baseline\n\nValidate the architecture and security boundaries.\n\n## Hidden\n\n不得修改接口。\n",
                            encoding="utf-8")
            with self.in_dir(root), self.assertRaises(harness.HarnessError):
                harness.start_stage(argparse.Namespace(stage="stage-1", slice="slice-1",
                                                       title="Baseline Validation", plan_file="stage-plan.md"))
        finally:
            temp.cleanup()

    def test_dry_run_manifest_matches_actual_project_mutations(self):
        temp, root = self.git_project()
        try:
            report, analyst, auditor, _final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            before = {str(path.relative_to(root)): path.read_bytes()
                      for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
            args = self.adoption_args(report, analyst, auditor, approved=True,
                                      approved_plan_hash=plan["plan_sha256"])
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(args)
            after = {str(path.relative_to(root)): path.read_bytes()
                     for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
            changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
            approved = {entry["path"] for entry in plan["operations"]
                        if entry["action"] in {"CREATE", "MODIFY"} and not entry["path"].endswith("/")
                        and not entry["path"].startswith("@git/")}
            self.assertEqual(changed, approved)
        finally:
            temp.cleanup()

    def test_canonical_manifest_binds_prior_desired_and_operation_bytes(self):
        temp, root = self.git_project()
        try:
            report, analyst, auditor, _final = self.write_reports(root)
            baseline = self.approved_plan(root, report, analyst, auditor)
            required = {"repository_id", "repository_head", "path", "operation", "prior_exists",
                        "prior_sha256", "desired_sha256", "prior_mode", "desired_mode"}
            self.assertTrue(all(required <= set(entry) for entry in baseline["mutation_manifest"]))
            ignore_entry = next(item for item in baseline["mutation_manifest"] if item["path"] == ".gitignore")
            self.assertEqual(ignore_entry["operation"], "CREATE")

            (root / ".gitignore").write_text("existing rule\n", encoding="utf-8")
            text, value, _audit, hashes = harness.validated_adoption_inputs(
                root, self.adoption_args(report, analyst, auditor))
            modified = harness.build_adoption_plan(root, text, value, hashes)
            changed = next(item for item in modified["mutation_manifest"] if item["path"] == ".gitignore")
            self.assertEqual(changed["operation"], "MODIFY")
            self.assertNotEqual(baseline["plan_sha256"], modified["plan_sha256"])
            old_hash = modified["plan_sha256"]
            (root / ".gitignore").write_text("different existing bytes\n", encoding="utf-8")
            text, value, _audit, hashes = harness.validated_adoption_inputs(
                root, self.adoption_args(report, analyst, auditor))
            current = harness.build_adoption_plan(root, text, value, hashes)
            self.assertNotEqual(old_hash, current["plan_sha256"])

            with mock.patch.object(harness, "existing_text_for_manifest", return_value="desired base\n"):
                desired_changed = harness.build_adoption_plan(root, text, value, hashes)
            self.assertNotEqual(current["plan_sha256"], desired_changed["plan_sha256"])
            tampered = json.loads(json.dumps(modified["plan_core"] if "plan_core" in modified else {
                key: modified[key] for key in ("lifecycle_version", "repository", "bindings", "protected_records",
                                                "configured_protected_paths", "context_records", "quality_gates",
                                                "quality_gate_inputs", "generation_id", "generated_at", "mutation_manifest")
            }))
            tampered["mutation_manifest"][0]["operation"] = "MODIFY"
            self.assertNotEqual(modified["plan_sha256"], harness.sha256_value(tampered))
        finally:
            temp.cleanup()

    def test_gate_argv_executables_and_approved_scope_change_plan_hash(self):
        for project_type in ("verify", "npm"):
            temp, root = self.git_project()
            try:
                if project_type == "verify":
                    (root / "Cargo.toml").write_text("[package]\nname='sample'\nversion='0.1.0'\n", encoding="utf-8")
                    (root / "scripts").mkdir()
                    (root / "scripts/verify").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    os.chmod(root / "scripts/verify", 0o755)
                    changed_path = root / "scripts/verify"
                    changed_bytes = "#!/bin/sh\nexit 1\n"
                else:
                    (root / "package.json").write_text(json.dumps({"scripts": {"test": "node test.js"}}), encoding="utf-8")
                    changed_path = root / "package.json"
                    changed_bytes = json.dumps({"scripts": {"test": "node changed.js"}})
                subprocess.run(["git", "add", "."], cwd=root, check=True)
                subprocess.run(["git", "commit", "-qm", "Add build metadata"], cwd=root, check=True)
                report, analyst_path, auditor_path, _final = self.write_reports(root)
                text, analyst, _audit, hashes = harness.validated_adoption_inputs(
                    root, self.adoption_args(report, analyst_path, auditor_path))
                first = harness.build_adoption_plan(root, text, analyst, hashes)
                changed_path.write_text(changed_bytes, encoding="utf-8")
                second = harness.build_adoption_plan(root, text, analyst, hashes)
                self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])
                self.assertNotEqual(first["quality_gate_inputs"], second["quality_gate_inputs"])

                with mock.patch.object(harness, "derived_gate_commands",
                                       return_value={"fast": [], "slice": [["git", "diff", "--check"]], "stage": []}):
                    argv_changed = harness.build_adoption_plan(root, text, analyst, hashes)
                self.assertNotEqual(second["plan_sha256"], argv_changed["plan_sha256"])
                protected_changed = json.loads(json.dumps(analyst))
                protected_changed["protected_paths"] = []
                self.assertNotEqual(second["plan_sha256"], harness.build_adoption_plan(
                    root, text, protected_changed, hashes)["plan_sha256"])
                context_changed = json.loads(json.dumps(analyst))
                context_changed["context_files"] = []
                self.assertNotEqual(second["plan_sha256"], harness.build_adoption_plan(
                    root, text, context_changed, hashes)["plan_sha256"])
            finally:
                temp.cleanup()
    def test_stale_approval_rejected_before_project_mutation(self):
        temp, root = self.git_project()
        try:
            report, analyst, auditor, _final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            (root / ".gitignore").write_text("operator change\n", encoding="utf-8")
            before = (root / ".gitignore").read_bytes()
            args = self.adoption_args(report, analyst, auditor, approved=True,
                                      approved_plan_hash=plan["plan_sha256"])
            with self.in_dir(root), self.assertRaises(harness.HarnessError):
                harness.adopt_project(args)
            self.assertEqual((root / ".gitignore").read_bytes(), before)
            self.assertFalse((root / ".harness").exists())
            self.assertFalse(harness.adoption_control(root)[0].exists())
        finally:
            temp.cleanup()

    def test_preserve_operation_does_not_rewrite_or_chmod_file(self):
        temp, root = self.git_project()
        try:
            (root / ".gitignore").write_text("/.harness/runtime/\n", encoding="utf-8")
            os.chmod(root / ".gitignore", 0o600)
            report, analyst, auditor, _final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            entry = next(item for item in plan["mutation_manifest"] if item["path"] == ".gitignore")
            self.assertEqual(entry["operation"], "PRESERVE")
            before = ((root / ".gitignore").read_bytes(), (root / ".gitignore").stat().st_mtime_ns,
                      stat.S_IMODE((root / ".gitignore").stat().st_mode))
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(self.adoption_args(report, analyst, auditor, approved=True,
                                                         approved_plan_hash=plan["plan_sha256"]))
            after = ((root / ".gitignore").read_bytes(), (root / ".gitignore").stat().st_mtime_ns,
                     stat.S_IMODE((root / ".gitignore").stat().st_mode))
            self.assertEqual(before, after)
        finally:
            temp.cleanup()

    def test_gate_input_change_is_rejected_before_gate_execution(self):
        temp, root = self.git_project()
        try:
            (root / "Cargo.toml").write_text("[package]\nname='sample'\nversion='0.1.0'\n", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts/verify").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(root / "scripts/verify", 0o755)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "Add verification entrypoint"], cwd=root, check=True)
            report, analyst, auditor, _final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(self.adoption_args(report, analyst, auditor, approved=True,
                                                         approved_plan_hash=plan["plan_sha256"]))
            p = harness.paths(root)
            for path in (p["state"], p["trusted_state"]):
                state = json.loads(path.read_text())
                state["workflow_state"] = "VALIDATING"
                path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            (root / "scripts/verify").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            with self.in_dir(root), mock.patch.object(harness, "run_logged") as runner, \
                    self.assertRaisesRegex(harness.HarnessError, "changed before gate execution"):
                harness.run_gates(argparse.Namespace(level="stage"))
            runner.assert_not_called()
        finally:
            temp.cleanup()

    def test_bootstrap_dry_run_lists_gitignore_and_matches_actual_mutations(self):
        with tempfile.TemporaryDirectory(prefix="BootstrapProject-") as raw:
            root = pathlib.Path(raw)
            brief = root / "brief-source.md"
            brief.write_text(
                "# Project Brief\n\nBuild a small service for test users with secure local data, "
                "a documented deployment process, explicit non-goals, and deterministic acceptance criteria.\n",
                encoding="utf-8")
            plan = harness.build_bootstrap_plan(root, brief.read_text(encoding="utf-8"))
            self.assertIn(".gitignore", {entry["path"] for entry in plan["operations"]})
            self.assertTrue(all(entry["action"] in {"CREATE", "MODIFY", "PRESERVE", "REJECT"}
                                for entry in plan["operations"]))
            before = {str(path.relative_to(root)): path.read_bytes()
                      for path in root.rglob("*") if path.is_file()}
            args = argparse.Namespace(brief=str(brief), approved=True,
                                      approved_plan_hash=plan["plan_sha256"], dry_run=False,
                                      operator_input="", operator_language="en")
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.bootstrap_project(args)
            after = {str(path.relative_to(root)): path.read_bytes()
                     for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
            changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
            approved = {entry["path"] for entry in plan["operations"]
                        if entry["action"] in {"CREATE", "MODIFY"} and not entry["path"].endswith("/")
                        and not entry["path"].startswith("@git/")}
            self.assertEqual(changed, approved)

    def test_fault_after_every_adoption_write_resumes_real_journal(self):
        labels = None
        for requested_label in [None]:
            temp, root = self.git_project()
            try:
                report, analyst, auditor, _final = self.write_reports(root)
                plan = self.approved_plan(root, report, analyst, auditor)
                public, trusted = harness.adoption_desired_files(
                    root, plan, report.read_text(encoding="utf-8"), json.loads(analyst.read_text()), "0" * 32)
                fake = {"files": ([{"area": "project", "path": path} for path in public] +
                                  [{"area": "trusted", "path": path} for path in trusted])}
                phases = harness.adoption_fault_phases(fake)
                rollback_phases = {"rollback_executing", "rollback_archive_recorded",
                                   "rollback_human_checkpoint", "rollback_complete",
                                   "rollback_gc_prepared", "rollback_gc_detached",
                                   "rollback_gc_blocked"}
                labels = [phase for phase in phases
                          if phase not in rollback_phases
                          and not phase.startswith(("activation_", "canonical_", "lifecycle_"))]
                activation = {phase for phase in phases
                              if phase.startswith(("activation_", "canonical_", "lifecycle_"))}
                self.assertEqual(set(phases), set(labels) | activation | rollback_phases)
            finally:
                temp.cleanup()
        assert labels is not None
        for label in labels:
            temp, root = self.git_project()
            try:
                report, analyst, auditor, _final = self.write_reports(root)
                plan = self.approved_plan(root, report, analyst, auditor)
                args = self.adoption_args(report, analyst, auditor, approved=True,
                                          approved_plan_hash=plan["plan_sha256"])
                with self.in_dir(root), mock.patch.dict(os.environ, {"HARNESS_ADOPTION_FAULT_AFTER": label}), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(harness.HarnessError):
                    harness.adopt_project(args)
                journal_path, _ = harness.adoption_control(root)
                self.assertIn(harness.load_json(journal_path)["lifecycle_state"],
                              {"ADOPTION_PLANNED", "ADOPTION_INTERRUPTED"})
                resume = self.adoption_args(None, None, None, resume=True)
                with self.in_dir(root), mock.patch.dict(os.environ, {}, clear=False), \
                        contextlib.redirect_stdout(io.StringIO()):
                    os.environ.pop("HARNESS_ADOPTION_FAULT_AFTER", None)
                    harness.adopt_project(resume)
                self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "ADOPTION_VALIDATING")
                self.assertEqual(harness.load_json(root / ".harness/lifecycle.json")["lifecycle_state"],
                                 "ADOPTION_VALIDATING")
            finally:
                temp.cleanup()

    def interrupted_adoption(self, root):
        report, analyst, auditor, _final = self.write_reports(root)
        plan = self.approved_plan(root, report, analyst, auditor)
        args = self.adoption_args(report, analyst, auditor, approved=True,
                                  approved_plan_hash=plan["plan_sha256"])
        with self.in_dir(root), mock.patch.dict(
                os.environ, {"HARNESS_ADOPTION_FAULT_AFTER": "install_file:trusted:lifecycle.json"}), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(harness.HarnessError):
            harness.adopt_project(args)
        return harness.adoption_control(root)[0]

    def rollback_record(self, journal_path, relative):
        return next(item for item in harness.load_json(journal_path)["backups"]
                    if item["path"] == relative)

    def mutate_canonical_journal(self, journal_path, marker, *, use_cas=False):
        journal = harness.load_json(journal_path)
        journal["external_marker"] = marker
        if use_cas:
            harness.save_adoption_journal(
                journal_path, journal, "rollback_human_checkpoint",
                expected_revision=int(journal["journal_revision"]),
                expected_digest=harness.sha256_value(harness.load_json(journal_path)))
        else:
            journal["journal_revision"] = int(journal.get("journal_revision", 0)) + 1
            harness.atomic_json(journal_path, journal)

    def interrupted_adoption_with_two_prior_files(self, root):
        (root / ".gitignore").write_text("operator ignore\n", encoding="utf-8")
        (root / "HARNESS_ADOPTION_REPORT.md").write_text(
            "# Existing Adoption Report\n\nOperator-owned prior report.\n", encoding="utf-8")
        return self.interrupted_adoption(root)

    def completed_rollback(self, root):
        target = root / ".gitignore"
        target.write_text("prior rollback bytes\n", encoding="utf-8")
        os.chmod(target, 0o640)
        journal_path = self.interrupted_adoption(root)
        with self.in_dir(root):
            harness.rollback_adoption(root)
        return journal_path, target

    def rollback_gc_roots(self, journal):
        control = pathlib.Path(journal["repository"]["git_dir"]) / "harness-control"
        generation = journal["generation_id"]
        digest = journal["rollback_archive"]["inventory_sha256"]
        return (control / f"rollback-archive/{generation}",
                control / f"rollback-retained/{generation}-{digest}")

    def test_adoption_rollback_restores_bytes_modes_absence_and_is_idempotent(self):
        temp, root = self.git_project()
        try:
            original = b"operator ignore bytes\n"
            (root / ".gitignore").write_bytes(original)
            os.chmod(root / ".gitignore", 0o640)
            journal_path = self.interrupted_adoption(root)
            staging = pathlib.Path(harness.load_json(journal_path)["staging_root"])
            shutil.rmtree(staging)
            with self.in_dir(root), mock.patch.dict(
                    os.environ, {"HARNESS_ADOPTION_FAULT_AFTER": "rollback_complete"}), \
                    self.assertRaises(harness.HarnessError):
                harness.rollback_adoption(root)
            with self.in_dir(root), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HARNESS_ADOPTION_FAULT_AFTER", None)
                harness.rollback_adoption(root)
            self.assertEqual((root / ".gitignore").read_bytes(), original)
            self.assertEqual(stat.S_IMODE((root / ".gitignore").stat().st_mode), 0o640)
            self.assertFalse((root / "HARNESS_ADOPTION_REPORT.md").exists())
            self.assertFalse((root / ".harness").exists())
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_adoption_rollback_cas_conflict_enters_human_checkpoint(self):
        temp, root = self.git_project()
        try:
            journal_path = self.interrupted_adoption(root)
            (root / "HARNESS_ADOPTION_REPORT.md").write_text("operator replacement\n", encoding="utf-8")
            with self.in_dir(root), mock.patch.dict(
                    os.environ, {"HARNESS_ADOPTION_FAULT_AFTER": "rollback_human_checkpoint"}), \
                    self.assertRaises(harness.HarnessError):
                harness.rollback_adoption(root)
            self.assertEqual((root / "HARNESS_ADOPTION_REPORT.md").read_text(), "operator replacement\n")
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "HUMAN_CHECKPOINT")
        finally:
            temp.cleanup()

    def test_adoption_rollback_validates_backup_before_accepting_prior_state(self):
        for damage in ("missing", "wrong-digest", "hardlink"):
            with self.subTest(damage=damage):
                temp, root = self.git_project()
                try:
                    original = b"operator ignore bytes\n"
                    target = root / ".gitignore"
                    target.write_bytes(original)
                    os.chmod(target, 0o640)
                    journal_path = self.interrupted_adoption(root)
                    record = self.rollback_record(journal_path, ".gitignore")
                    target.write_bytes(original)
                    os.chmod(target, 0o640)
                    backup = pathlib.Path(record["backup_path"])
                    if damage == "missing":
                        backup.unlink()
                    elif damage == "wrong-digest":
                        backup.write_bytes(b"damaged backup\n")
                    else:
                        second_link = backup.with_name(backup.name + ".link")
                        os.link(backup, second_link)
                    with self.in_dir(root), self.assertRaises(harness.HarnessError):
                        harness.rollback_adoption(root)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                        "ADOPTION_ROLLED_BACK")
                finally:
                    temp.cleanup()

    def test_adoption_rollback_global_preflight_prevents_earlier_mutation(self):
        for damage in ("missing", "digest", "mode"):
            with self.subTest(damage=damage):
                temp, root = self.git_project()
                try:
                    journal_path = self.interrupted_adoption_with_two_prior_files(root)
                    earlier_target = root / "HARNESS_ADOPTION_REPORT.md"
                    before = harness.snapshot_rollback_file(earlier_target, "test target")[1]
                    later = self.rollback_record(journal_path, ".gitignore")
                    backup = pathlib.Path(later["backup_path"])
                    if damage == "missing":
                        backup.unlink()
                    elif damage == "digest":
                        backup.write_text("damaged\n", encoding="utf-8")
                    else:
                        os.chmod(backup, 0o640)
                    with self.in_dir(root), self.assertRaisesRegex(
                            harness.HarnessError, "preflight"):
                        harness.rollback_adoption(root)
                    after = harness.snapshot_rollback_file(earlier_target, "test target")[1]
                    self.assertEqual(after, before)
                    self.assertEqual(harness.load_json(journal_path)["lifecycle_state"],
                                     "HUMAN_CHECKPOINT")
                finally:
                    temp.cleanup()

    def test_rollback_journal_change_after_preflight_blocks_all_project_mutation(self):
        for point in ("after-preflight", "after-plan-generation"):
            with self.subTest(point=point):
                temp, root = self.git_project()
                try:
                    target = root / ".gitignore"
                    target.write_text("prior\n", encoding="utf-8")
                    journal_path = self.interrupted_adoption(root)
                    before = harness.snapshot_rollback_file(target, "test target")[1]
                    original = harness.build_rollback_execution_plan

                    def mutate_after_plan(project, journal):
                        plan = original(project, journal)
                        self.mutate_canonical_journal(journal_path, point)
                        return plan

                    with self.in_dir(root), mock.patch.object(
                            harness, "build_rollback_execution_plan", side_effect=mutate_after_plan), \
                            self.assertRaisesRegex(harness.HarnessError, "CAS conflict"):
                        harness.rollback_adoption(root)
                    self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
                    self.assertEqual(harness.load_json(journal_path)["external_marker"], point)
                finally:
                    temp.cleanup()

    def test_rollback_journal_change_immediately_before_cas_is_not_overwritten(self):
        temp, root = self.git_project()
        try:
            target = root / ".gitignore"
            target.write_text("prior\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            original = harness.transition_rollback_executing

            def mutate_before_cas(project, path, plan):
                self.mutate_canonical_journal(path, "immediately-before-cas")
                return original(project, path, plan)

            with self.in_dir(root), mock.patch.object(
                    harness, "transition_rollback_executing", side_effect=mutate_before_cas), \
                    self.assertRaisesRegex(harness.HarnessError, "CAS conflict"):
                harness.rollback_adoption(root)
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
            self.assertEqual(harness.load_json(journal_path)["external_marker"],
                             "immediately-before-cas")
        finally:
            temp.cleanup()

    def test_concurrent_harness_process_journal_cas_wins_without_project_mutation(self):
        temp, root = self.git_project()
        try:
            target = root / ".gitignore"
            target.write_text("prior\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            original = harness.build_rollback_execution_plan

            def concurrent_writer():
                with harness.state_lock(harness.paths(root)):
                    self.mutate_canonical_journal(
                        journal_path, "concurrent-harness-process", use_cas=True)

            def write_between_preflight_and_lock(project, journal):
                plan = original(project, journal)
                process = multiprocessing.get_context("fork").Process(target=concurrent_writer)
                process.start()
                process.join(10)
                self.assertEqual(process.exitcode, 0)
                return plan

            with self.in_dir(root), mock.patch.object(
                    harness, "build_rollback_execution_plan",
                    side_effect=write_between_preflight_and_lock), \
                    self.assertRaisesRegex(harness.HarnessError, "CAS conflict"):
                harness.rollback_adoption(root)
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
            self.assertEqual(harness.load_json(journal_path)["external_marker"],
                             "concurrent-harness-process")
        finally:
            temp.cleanup()

    def test_atomic_restore_preserves_edit_immediately_after_preflight(self):
        temp, root = self.git_project()
        try:
            target = root / ".gitignore"
            target.write_text("prior\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            original_preflight = harness.build_rollback_execution_plan

            def edit_after_preflight(project, journal):
                plan = original_preflight(project, journal)
                target.write_text("operator after preflight\n", encoding="utf-8")
                return plan

            with self.in_dir(root), mock.patch.object(
                    harness, "build_rollback_execution_plan", side_effect=edit_after_preflight), \
                    self.assertRaisesRegex(harness.HarnessError, "displaced-object"):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_text(encoding="utf-8"), "operator after preflight\n")
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "HUMAN_CHECKPOINT")
        finally:
            temp.cleanup()

    def test_atomic_restore_preserves_target_swap_immediately_before_exchange(self):
        temp, root = self.git_project()
        try:
            target = root / ".gitignore"
            target.write_text("prior\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            original_exchange = harness.linux_renameat2
            swapped_inode = None

            def swap_before_exchange(directory_fd, old_name, new_name, flags):
                nonlocal swapped_inode
                if (flags == harness.RENAME_EXCHANGE and old_name != new_name and
                        new_name == target.name and swapped_inode is None):
                    replacement = root / "operator-swap"
                    replacement.write_bytes(target.read_bytes())
                    os.chmod(replacement, stat.S_IMODE(target.stat().st_mode))
                    os.replace(replacement, target)
                    swapped_inode = target.stat().st_ino
                return original_exchange(directory_fd, old_name, new_name, flags)

            with self.in_dir(root), mock.patch.object(
                    harness, "linux_renameat2", side_effect=swap_before_exchange), \
                    self.assertRaisesRegex(harness.HarnessError, "displaced-object"):
                harness.rollback_adoption(root)
            record = self.rollback_record(journal_path, ".gitignore")
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                             record["desired_sha256"])
            self.assertEqual(target.stat().st_ino, swapped_inode)
            self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_atomic_quarantine_preserves_concurrent_edit(self):
        temp, root = self.git_project()
        try:
            journal_path = self.interrupted_adoption(root)
            target = root / "HARNESS_ADOPTION_REPORT.md"
            original_remove = harness.execute_remove_action

            def edit_before_quarantine(action, archive_root, generation_id):
                if action.path == "HARNESS_ADOPTION_REPORT.md":
                    target.write_text("operator removal race\n", encoding="utf-8")
                return original_remove(action, archive_root, generation_id)

            with self.in_dir(root), mock.patch.object(
                    harness, "execute_remove_action", side_effect=edit_before_quarantine), \
                    self.assertRaisesRegex(harness.HarnessError, "quarantined-object"):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_text(encoding="utf-8"), "operator removal race\n")
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "HUMAN_CHECKPOINT")
        finally:
            temp.cleanup()

    def test_failed_displaced_verification_atomically_restores_concurrent_edit(self):
        temp, root = self.git_project()
        try:
            target = root / ".gitignore"
            target.write_text("prior\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            original_exchange = harness.linux_renameat2
            modified = False

            def modify_displaced(directory_fd, old_name, new_name, flags):
                nonlocal modified
                result = original_exchange(directory_fd, old_name, new_name, flags)
                if flags == harness.RENAME_EXCHANGE and old_name != new_name and not modified:
                    modified = True
                    fd = os.open(old_name, os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fd)
                    try:
                        os.write(fd, b"operator displaced edit\n")
                    finally:
                        os.close(fd)
                return result

            with self.in_dir(root), mock.patch.object(
                    harness, "linux_renameat2", side_effect=modify_displaced), \
                    self.assertRaisesRegex(harness.HarnessError, "displaced-object"):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_text(encoding="utf-8"), "operator displaced edit\n")
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "HUMAN_CHECKPOINT")
        finally:
            temp.cleanup()

    def test_atomic_rollback_capability_failure_makes_no_target_change(self):
        temp, root = self.git_project()
        try:
            target = root / ".gitignore"
            target.write_text("prior\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            with self.in_dir(root), mock.patch.object(
                    harness, "ensure_atomic_exchange_capability",
                    side_effect=harness.HarnessError("atomic rollback capability unavailable")), \
                    self.assertRaisesRegex(harness.HarnessError, "capability unavailable"):
                harness.rollback_adoption(root)
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "HUMAN_CHECKPOINT")
        finally:
            temp.cleanup()

    def test_multi_record_runtime_failure_never_publishes_partial_success(self):
        temp, root = self.git_project()
        try:
            journal_path = self.interrupted_adoption_with_two_prior_files(root)
            original_execute = harness.execute_rollback_action
            calls = 0

            def fail_later(action, archive_root, generation_id):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise harness.HarnessError("deterministic later record failure")
                return original_execute(action, archive_root, generation_id)

            with self.in_dir(root), mock.patch.object(
                    harness, "execute_rollback_action", side_effect=fail_later), \
                    self.assertRaisesRegex(harness.HarnessError, "later record failure"):
                harness.rollback_adoption(root)
            self.assertGreaterEqual(calls, 2)
            self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_completed_rollback_retains_complete_generation_archive_inventory(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            journal = harness.load_json(journal_path)
            archive = journal["rollback_archive"]
            archive_root = (pathlib.Path(journal["repository"]["git_dir"]) /
                            "harness-control" / archive["relative_root"])
            self.assertEqual(journal["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertTrue(archive["project_state_restored"])
            self.assertEqual(archive["gc_phase"], "GC_NOT_STARTED")
            self.assertTrue(archive["inventory"])
            self.assertEqual({item.name for item in archive_root.iterdir()},
                             {item["relative_name"] for item in archive["inventory"]})
            for record in archive["inventory"]:
                state = harness.snapshot_archive_object(
                    archive_root / record["relative_name"], "test archive object")
                self.assertEqual(state, harness.archive_state_from_record(record))
                self.assertEqual(record["generation_id"], journal["generation_id"])
            self.assertEqual(target.read_text(encoding="utf-8"), "prior rollback bytes\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        finally:
            temp.cleanup()

    def test_rollback_archive_inventory_tampering_and_unjournaled_objects_block_success(self):
        for tamper in ("inventory", "unjournaled"):
            with self.subTest(tamper=tamper):
                temp, root = self.git_project()
                try:
                    journal_path, _target = self.completed_rollback(root)
                    journal = harness.load_json(journal_path)
                    archive_root = (pathlib.Path(journal["repository"]["git_dir"]) /
                                    "harness-control" / journal["rollback_archive"]["relative_root"])
                    if tamper == "inventory":
                        journal["rollback_archive"]["inventory"][0]["sha256"] = "0" * 64
                        journal["journal_revision"] += 1
                        harness.atomic_json(journal_path, journal)
                    else:
                        (archive_root / "unjournaled-object").write_text("unexpected\n", encoding="utf-8")
                    with self.in_dir(root), self.assertRaisesRegex(
                            harness.HarnessError, "archive"):
                        harness.rollback_adoption(root)
                    self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                        "ADOPTION_ROLLED_BACK")
                finally:
                    temp.cleanup()

    def test_displaced_object_rename_or_temporary_replacement_never_publishes_success(self):
        for race in ("rename", "replace"):
            with self.subTest(race=race):
                temp, root = self.git_project()
                try:
                    target = root / ".gitignore"
                    target.write_text("prior\n", encoding="utf-8")
                    journal_path = self.interrupted_adoption(root)
                    original_move = harness.move_object_to_rollback_archive
                    preserved = root / f"preserved-{race}"
                    injected = False

                    def race_archive(source_parent, source_name, archive_root, archive_name,
                                     action_path, generation_id, role):
                        nonlocal injected
                        if action_path == ".gitignore" and not injected:
                            injected = True
                            source = source_parent / source_name
                            if race == "rename":
                                os.rename(source, preserved)
                            else:
                                preserved.write_text("operator temporary replacement\n",
                                                     encoding="utf-8")
                                directory_fd = harness.open_directory_nofollow(source_parent)
                                try:
                                    harness.linux_renameat2(
                                        directory_fd, preserved.name, source_name,
                                        harness.RENAME_EXCHANGE)
                                finally:
                                    os.close(directory_fd)
                        return original_move(source_parent, source_name, archive_root, archive_name,
                                             action_path, generation_id, role)

                    with self.in_dir(root), mock.patch.object(
                            harness, "move_object_to_rollback_archive", side_effect=race_archive), \
                            self.assertRaises((harness.HarnessError, OSError)):
                        harness.rollback_adoption(root)
                    self.assertTrue(preserved.exists())
                    self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                        "ADOPTION_ROLLED_BACK")
                finally:
                    temp.cleanup()

    def test_temporary_directory_replacement_blocks_archive_accounting(self):
        temp, root = self.git_project()
        try:
            journal_path = self.interrupted_adoption(root)
            original = harness.execute_rollback_directory_action
            preserved = root / "preserved-harness-directory"

            def replace_directory(action, archive_root, generation_id):
                if action.path == ".harness/":
                    os.rename(action.target, preserved)
                    action.target.mkdir(mode=0o755)
                return original(action, archive_root, generation_id)

            with self.in_dir(root), mock.patch.object(
                    harness, "execute_rollback_directory_action", side_effect=replace_directory), \
                    self.assertRaisesRegex(harness.HarnessError, "directory identity"):
                harness.rollback_adoption(root)
            self.assertTrue(preserved.is_dir())
            self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_rollback_archive_gc_is_explicit_repeatable_and_preserves_project(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            original_state = harness.snapshot_rollback_file(target, "test target")[1]
            with self.in_dir(root):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
                harness.garbage_collect_rollback_archive(argparse.Namespace())
                harness.rollback_adoption(root)
            journal = harness.load_json(journal_path)
            archive_root, retained_root = self.rollback_gc_roots(journal)
            self.assertEqual(journal["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertEqual(journal["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertFalse(archive_root.exists())
            self.assertTrue(retained_root.is_dir())
            self.assertEqual({item.name for item in retained_root.iterdir()},
                             {item["relative_name"]
                              for item in journal["rollback_archive"]["inventory"]})
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1],
                             original_state)
        finally:
            temp.cleanup()

    def test_rollback_archive_gc_failure_does_not_change_restored_project_state(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            with self.in_dir(root), mock.patch.object(
                    harness, "linux_renameat2_between", side_effect=OSError("gc failure")), \
                    self.assertRaisesRegex(OSError, "gc failure"):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            journal = harness.load_json(journal_path)
            self.assertEqual(journal["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertNotEqual(journal["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
        finally:
            temp.cleanup()

    def test_rollback_gc_recovers_after_every_crash_boundary(self):
        boundaries = [
            "before_gc_prepared", "gc_prepared", "retained_parent_durable", "archive_detached",
            "before_gc_detached", "gc_detached",
        ]
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                temp, root = self.git_project()
                try:
                    journal_path, target = self.completed_rollback(root)
                    before = harness.snapshot_rollback_file(target, "test target")[1]
                    with self.in_dir(root), mock.patch.dict(
                            os.environ, {"HARNESS_ROLLBACK_GC_FAULT_AFTER": boundary}), \
                            self.assertRaisesRegex(harness.RollbackGCFault, boundary):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    interrupted = harness.load_json(journal_path)
                    self.assertEqual(interrupted["lifecycle_state"], "ADOPTION_ROLLED_BACK")
                    with self.in_dir(root):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    detached = harness.load_json(journal_path)
                    archive_root, retained_root = self.rollback_gc_roots(detached)
                    self.assertEqual(detached["lifecycle_state"], "ADOPTION_ROLLED_BACK")
                    self.assertEqual(detached["rollback_archive"]["gc_phase"],
                                     "GC_DETACHED")
                    self.assertIn("gc_detached_at", detached["rollback_archive"])
                    self.assertFalse(archive_root.exists())
                    self.assertTrue(retained_root.is_dir())
                    self.assertEqual(
                        {item.name for item in retained_root.iterdir()},
                        {item["relative_name"]
                         for item in detached["rollback_archive"]["inventory"]})
                    self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1],
                                     before)
                finally:
                    temp.cleanup()

    def test_rollback_gc_reconciles_prepared_and_detached_uncommitted_states(self):
        for boundary in ("archive_detached", "gc_detached"):
            with self.subTest(boundary=boundary):
                temp, root = self.git_project()
                try:
                    journal_path, target = self.completed_rollback(root)
                    before = harness.snapshot_rollback_file(target, "test target")[1]
                    with self.in_dir(root), mock.patch.dict(
                            os.environ, {"HARNESS_ROLLBACK_GC_FAULT_AFTER": boundary}), \
                            self.assertRaises(harness.RollbackGCFault):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    interrupted = harness.load_json(journal_path)
                    archive_root, retained_root = self.rollback_gc_roots(interrupted)
                    self.assertFalse(archive_root.exists())
                    self.assertTrue(retained_root.exists())
                    with self.in_dir(root):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    detached = harness.load_json(journal_path)
                    self.assertEqual(detached["rollback_archive"]["gc_phase"],
                                     "GC_DETACHED")
                    self.assertTrue(retained_root.exists())
                    self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1],
                                     before)
                finally:
                    temp.cleanup()

    def test_rollback_gc_rejects_existing_destination_and_immediate_identity_failure(self):
        for conflict in ("retained-exists", "post-rename-identity"):
            with self.subTest(conflict=conflict):
                temp, root = self.git_project()
                try:
                    journal_path, target = self.completed_rollback(root)
                    before = harness.snapshot_rollback_file(target, "test target")[1]
                    journal = harness.load_json(journal_path)
                    archive_root, retained_root = self.rollback_gc_roots(journal)
                    retained_root.parent.mkdir(mode=0o700, exist_ok=True)
                    if conflict == "retained-exists":
                        retained_root.mkdir(mode=0o700)
                        (retained_root / "unrelated").write_text("do not replace\n",
                                                                encoding="utf-8")
                    original_identity = harness.gc_root_identity

                    def fail_immediate_retained_identity(path, label):
                        value = original_identity(path, label)
                        if conflict == "post-rename-identity" and path == retained_root:
                            value = dict(value)
                            value["inode"] += 1
                        return value

                    identity_patch = (mock.patch.object(
                        harness, "gc_root_identity", side_effect=fail_immediate_retained_identity)
                        if conflict == "post-rename-identity" else contextlib.nullcontext())
                    with self.in_dir(root), identity_patch, self.assertRaises(harness.HarnessError):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    failed = harness.load_json(journal_path)
                    self.assertEqual(failed["lifecycle_state"], "ADOPTION_ROLLED_BACK")
                    if conflict == "retained-exists":
                        self.assertEqual(failed["rollback_archive"]["gc_phase"],
                                         "GC_NOT_STARTED")
                        self.assertTrue(archive_root.exists())
                    else:
                        self.assertEqual(failed["rollback_archive"]["gc_phase"], "GC_BLOCKED")
                    if conflict == "retained-exists":
                        self.assertEqual((retained_root / "unrelated").read_text(),
                                         "do not replace\n")
                    else:
                        self.assertTrue(retained_root.exists())
                    self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1],
                                     before)
                finally:
                    temp.cleanup()

    def test_rollback_gc_rejects_cas_digest_generation_and_unjournaled_conflicts(self):
        for conflict in ("journal-cas", "inventory-digest", "generation", "unjournaled"):
            with self.subTest(conflict=conflict):
                temp, root = self.git_project()
                try:
                    journal_path, target = self.completed_rollback(root)
                    before = harness.snapshot_rollback_file(target, "test target")[1]
                    journal = harness.load_json(journal_path)
                    archive_root, _retained_root = self.rollback_gc_roots(journal)
                    if conflict == "inventory-digest":
                        journal["rollback_archive"]["inventory_sha256"] = "0" * 64
                        journal["journal_revision"] += 1
                        harness.atomic_json(journal_path, journal)
                    elif conflict == "generation":
                        journal["rollback_archive"]["generation_id"] = "wrong-generation"
                        journal["journal_revision"] += 1
                        harness.atomic_json(journal_path, journal)
                    archive_before = {
                        item.name: harness.snapshot_archive_object(item, "test GC archive object")
                        for item in archive_root.iterdir()
                    }

                    original_transition = harness.gc_transition

                    def conflict_before_cas(path, authoritative, phase, durability, values=None):
                        if phase == "GC_PREPARED":
                            changed = harness.load_json(path)
                            changed["external_gc_writer"] = True
                            changed["journal_revision"] += 1
                            harness.atomic_json(path, changed)
                        return original_transition(path, authoritative, phase, durability, values)

                    transition_patch = (mock.patch.object(
                        harness, "gc_transition", side_effect=conflict_before_cas)
                        if conflict == "journal-cas" else contextlib.nullcontext())
                    original_detach = harness.detach_rollback_gc

                    def insert_before_detach(*args, **kwargs):
                        (archive_root / "unjournaled").write_text(
                            "preserve me\n", encoding="utf-8")
                        return original_detach(*args, **kwargs)

                    detach_patch = (mock.patch.object(
                        harness, "detach_rollback_gc", side_effect=insert_before_detach)
                        if conflict == "unjournaled" else contextlib.nullcontext())
                    with self.in_dir(root), transition_patch, detach_patch, \
                            self.assertRaises(harness.HarnessError):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    failed = harness.load_json(journal_path)
                    self.assertEqual(failed["lifecycle_state"], "ADOPTION_ROLLED_BACK")
                    self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1],
                                     before)
                    archive_after = {
                        item.name: harness.snapshot_archive_object(item, "test GC archive object")
                        for item in archive_root.iterdir()
                    }
                    for name, state in archive_before.items():
                        self.assertEqual(archive_after[name], state)
                    if conflict != "unjournaled":
                        self.assertEqual(archive_after, archive_before)
                    if conflict == "journal-cas":
                        self.assertTrue(failed["external_gc_writer"])
                    if conflict == "unjournaled":
                        self.assertEqual((archive_root / "unjournaled").read_text(),
                                         "preserve me\n")
                finally:
                    temp.cleanup()

    def test_recreated_active_archive_is_unbound_warned_and_preserved(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            with self.in_dir(root):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            detached = harness.load_json(journal_path)
            archive_root, retained_root = self.rollback_gc_roots(detached)
            retained_identity = detached["rollback_archive"]["gc_retained_identity"]
            retained_names = {item.name for item in retained_root.iterdir()}
            archive_root.mkdir(mode=0o700)
            (archive_root / "arbitrary-unbound-data").write_text(
                "operator-owned replacement\n", encoding="utf-8")
            output = io.StringIO()
            with self.in_dir(root), contextlib.redirect_stdout(output):
                harness.status(argparse.Namespace(json=True))
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            status_value = json.loads(output.getvalue().splitlines()[0])
            repeated = harness.load_json(journal_path)
            self.assertIn("UNBOUND_ACTIVE_ARCHIVE_PRESENT", status_value["warnings"])
            self.assertEqual(repeated["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertEqual(repeated["rollback_archive"]["gc_retained_identity"],
                             retained_identity)
            self.assertEqual({item.name for item in retained_root.iterdir()}, retained_names)
            self.assertEqual((archive_root / "arbitrary-unbound-data").read_text(),
                             "operator-owned replacement\n")
            self.assertNotEqual(archive_root.stat().st_ino, retained_root.stat().st_ino)
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
        finally:
            temp.cleanup()

    def test_later_retained_archive_drift_is_diagnostic_and_preserves_history(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            with self.in_dir(root):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            completed = harness.load_json(journal_path)
            _archive_root, retained_root = self.rollback_gc_roots(completed)
            preserved = retained_root.with_name(retained_root.name + ".preserved")
            os.rename(retained_root, preserved)
            retained_root.mkdir(mode=0o700)
            (retained_root / "unrelated").write_text("do not adopt or delete\n", encoding="utf-8")
            output = io.StringIO()
            with self.in_dir(root), contextlib.redirect_stdout(output):
                harness.status(argparse.Namespace(json=True))
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            status_value = json.loads(output.getvalue().splitlines()[0])
            self.assertIn("RETAINED_ARCHIVE_DRIFT", status_value["warnings"])
            self.assertTrue(preserved.exists())
            self.assertTrue(retained_root.exists())
            self.assertEqual((retained_root / "unrelated").read_text(),
                             "do not adopt or delete\n")
            historical = harness.load_json(journal_path)
            self.assertEqual(historical["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertEqual(historical["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertEqual(historical["rollback_archive"]["gc_retained_identity"],
                             completed["rollback_archive"]["gc_retained_identity"])
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
        finally:
            temp.cleanup()

    def test_later_retained_inventory_drift_is_reported_without_mutation(self):
        temp, root = self.git_project()
        try:
            journal_path, _target = self.completed_rollback(root)
            with self.in_dir(root):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            detached = harness.load_json(journal_path)
            _archive_root, retained_root = self.rollback_gc_roots(detached)
            record = next(item for item in detached["rollback_archive"]["inventory"]
                          if item["object_type"] == "file")
            changed = retained_root / record["relative_name"]
            changed.write_bytes(b"accidental retained archive damage\n")
            changed_bytes = changed.read_bytes()

            output = io.StringIO()
            with self.in_dir(root), contextlib.redirect_stdout(output):
                harness.status(argparse.Namespace(json=True))
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            status_value = json.loads(output.getvalue().splitlines()[0])
            self.assertIn("RETAINED_ARCHIVE_DRIFT", status_value["warnings"])
            self.assertEqual(changed.read_bytes(), changed_bytes)
            historical = harness.load_json(journal_path)
            self.assertEqual(historical["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertEqual(historical["rollback_archive"]["gc_phase"], "GC_DETACHED")
        finally:
            temp.cleanup()

    def test_doctor_reports_later_retained_archive_drift_without_invoking_hermes(self):
        temp, root = self.git_project()
        try:
            journal_path, _target = self.completed_rollback(root)
            with self.in_dir(root):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            detached = harness.load_json(journal_path)
            _archive_root, retained_root = self.rollback_gc_roots(detached)
            preserved = retained_root.with_name(retained_root.name + ".doctor-preserved")
            os.rename(retained_root, preserved)
            retained_root.mkdir(mode=0o700)

            output = io.StringIO()
            fake_version = (True, "Hermes Agent v0.19.1")
            with self.in_dir(root), contextlib.redirect_stdout(output), \
                    mock.patch.object(harness, "executable_version", return_value=fake_version), \
                    mock.patch.object(harness.shutil, "which", return_value=None), \
                    self.assertRaises(harness.HarnessError):
                harness.doctor(argparse.Namespace(capabilities=False))
            self.assertIn("RETAINED_ARCHIVE_DRIFT", output.getvalue())
            historical = harness.load_json(journal_path)
            self.assertEqual(historical["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertEqual(historical["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertTrue(preserved.exists())
            self.assertTrue(retained_root.exists())
        finally:
            temp.cleanup()

    def test_cooperating_concurrent_gc_invocations_serialize_and_are_idempotent(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            context = multiprocessing.get_context("fork")
            results = context.Queue()

            def invoke(project, queue):
                try:
                    os.chdir(project)
                    harness.garbage_collect_rollback_archive(argparse.Namespace())
                    queue.put(None)
                except Exception as exc:  # pragma: no cover - child evidence path
                    queue.put(f"{type(exc).__name__}: {exc}")

            workers = [context.Process(target=invoke, args=(str(root), results))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(20)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual([results.get(timeout=2) for _ in workers], [None, None])
            detached = harness.load_json(journal_path)
            self.assertEqual(detached["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
        finally:
            temp.cleanup()

    def test_retained_parent_creation_and_detach_are_durable_before_phase_publish(self):
        temp, root = self.git_project()
        try:
            journal_path, _target = self.completed_rollback(root)
            journal = harness.load_json(journal_path)
            archive_root, retained_root = self.rollback_gc_roots(journal)
            control = retained_root.parent.parent
            inode_labels = {
                control.stat().st_ino: "control",
                archive_root.parent.stat().st_ino: "archive-parent",
            }
            fsync_events = []
            original_fsync = os.fsync
            original_transition = harness.gc_transition

            def record_fsync(fd):
                info = os.fstat(fd)
                label = inode_labels.get(info.st_ino)
                if label is None:
                    with contextlib.suppress(OSError):
                        opened_path = pathlib.Path(os.readlink(f"/proc/self/fd/{fd}"))
                        if opened_path.name == "rollback-retained":
                            label = "retained-parent"
                if label:
                    fsync_events.append(label)
                return original_fsync(fd)

            def assert_durable_before_phase(path, authoritative, phase,
                                            durability, values=None):
                if retained_root.parent.exists():
                    inode_labels[retained_root.parent.stat().st_ino] = "retained-parent"
                if phase == "GC_DETACHED":
                    self.assertIn("control", fsync_events)
                    self.assertIn("archive-parent", fsync_events)
                    self.assertIn("retained-parent", fsync_events)
                return original_transition(path, authoritative, phase, durability, values)

            with self.in_dir(root), mock.patch("os.fsync", side_effect=record_fsync), \
                    mock.patch.object(harness, "gc_transition",
                                      side_effect=assert_durable_before_phase):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            self.assertTrue(retained_root.exists())
            self.assertLess(fsync_events.index("retained-parent"),
                            fsync_events.index("control"))
        finally:
            temp.cleanup()

    def test_durable_directory_creation_reuses_directory_and_rejects_unsafe_names(self):
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw)
            existing = parent / "existing"
            existing.mkdir(mode=0o700)
            before = (existing.stat().st_ino, existing.stat().st_mtime_ns,
                      parent.stat().st_mtime_ns)
            fsync_spy = mock.Mock(wraps=os.fsync)
            mkdir_spy = mock.Mock(wraps=os.mkdir)
            with mock.patch("os.fsync", side_effect=fsync_spy), \
                    mock.patch("os.mkdir", side_effect=mkdir_spy):
                value, created = harness.ensure_durable_directory(parent, "existing")
            self.assertEqual(value, existing)
            self.assertFalse(created)
            self.assertEqual(mkdir_spy.call_count, 0)
            self.assertEqual(fsync_spy.call_count, 0)
            self.assertEqual((existing.stat().st_ino, existing.stat().st_mtime_ns,
                              parent.stat().st_mtime_ns), before)

            target = parent / "target"
            target.mkdir()
            (parent / "linked").symlink_to(target.name)
            (parent / "file").write_text("not a directory\n", encoding="utf-8")
            for name in ("linked", "file"):
                with self.subTest(name=name), self.assertRaises(harness.HarnessError):
                    harness.ensure_durable_directory(parent, name)

    def test_gc_rejects_symlink_or_non_directory_retained_parent(self):
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind):
                temp, root = self.git_project()
                try:
                    journal_path, target = self.completed_rollback(root)
                    before = harness.snapshot_rollback_file(target, "test target")[1]
                    journal = harness.load_json(journal_path)
                    archive_root, retained_root = self.rollback_gc_roots(journal)
                    if kind == "symlink":
                        alternate = retained_root.parent.with_name("unrelated-retained")
                        alternate.mkdir(mode=0o700)
                        retained_root.parent.symlink_to(alternate.name)
                    else:
                        retained_root.parent.write_text("not a directory\n", encoding="utf-8")
                    with self.in_dir(root), self.assertRaises(harness.HarnessError):
                        harness.garbage_collect_rollback_archive(argparse.Namespace())
                    self.assertTrue(archive_root.exists())
                    self.assertEqual(harness.load_json(journal_path)["lifecycle_state"],
                                     "ADOPTION_ROLLED_BACK")
                    self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1],
                                     before)
                finally:
                    temp.cleanup()

    def test_rollback_gc_never_physically_deletes_retained_objects(self):
        temp, root = self.git_project()
        try:
            journal_path, target = self.completed_rollback(root)
            before = harness.snapshot_rollback_file(target, "test target")[1]
            unlink_spy = mock.Mock(wraps=os.unlink)
            rmdir_spy = mock.Mock(wraps=os.rmdir)
            with self.in_dir(root), mock.patch("os.unlink", side_effect=unlink_spy), \
                    mock.patch("os.rmdir", side_effect=rmdir_spy):
                harness.garbage_collect_rollback_archive(argparse.Namespace())
                harness.garbage_collect_rollback_archive(argparse.Namespace())
            detached = harness.load_json(journal_path)
            archive_root, retained_root = self.rollback_gc_roots(detached)
            self.assertEqual(detached["rollback_archive"]["gc_phase"], "GC_DETACHED")
            self.assertFalse(archive_root.exists())
            self.assertTrue(retained_root.exists())
            inventory_names = {record["relative_name"]
                               for record in detached["rollback_archive"]["inventory"]}
            self.assertTrue(all(pathlib.Path(os.fspath(call.args[0])).name not in inventory_names
                                for call in unlink_spy.call_args_list))
            self.assertEqual(rmdir_spy.call_count, 0)
            for record in detached["rollback_archive"]["inventory"]:
                self.assertEqual(
                    harness.snapshot_archive_object(
                        retained_root / record["relative_name"], "retained object"),
                    harness.archive_state_from_record(record))
            self.assertEqual(harness.snapshot_rollback_file(target, "test target")[1], before)
        finally:
            temp.cleanup()

    def test_adoption_rollback_repairs_mode_when_prior_bytes_already_restored(self):
        temp, root = self.git_project()
        try:
            original = b"operator ignore bytes\n"
            target = root / ".gitignore"
            target.write_bytes(original)
            os.chmod(target, 0o640)
            journal_path = self.interrupted_adoption(root)
            target.write_bytes(original)
            os.chmod(target, 0o600)
            with self.in_dir(root):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"],
                             "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_adoption_rollback_accepts_exact_prior_state_after_backup_validation(self):
        temp, root = self.git_project()
        try:
            original = b"operator ignore bytes\n"
            target = root / ".gitignore"
            target.write_bytes(original)
            os.chmod(target, 0o640)
            journal_path = self.interrupted_adoption(root)
            target.write_bytes(original)
            os.chmod(target, 0o640)
            with self.in_dir(root):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"],
                             "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_adoption_rollback_restores_desired_bytes_and_exact_prior_mode(self):
        temp, root = self.git_project()
        try:
            original = b"operator ignore bytes\n"
            target = root / ".gitignore"
            target.write_bytes(original)
            os.chmod(target, 0o666)
            journal_path = self.interrupted_adoption(root)
            record = self.rollback_record(journal_path, ".gitignore")
            self.assertEqual(harness.read_rollback_file(target, "test target")[0],
                             pathlib.Path(record["backup_path"]).read_bytes() + b"/.harness/runtime/\n")
            with self.in_dir(root):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o666)
            with self.in_dir(root):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o666)
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"],
                             "ADOPTION_ROLLED_BACK")
        finally:
            temp.cleanup()

    def test_adoption_rollback_removes_only_exact_created_file_state(self):
        for operator_change in (None, "bytes", "mode"):
            with self.subTest(operator_change=operator_change):
                temp, root = self.git_project()
                try:
                    journal_path = self.interrupted_adoption(root)
                    target = root / "HARNESS_ADOPTION_REPORT.md"
                    if operator_change == "bytes":
                        target.write_text("operator replacement\n", encoding="utf-8")
                    elif operator_change == "mode":
                        os.chmod(target, 0o600)
                    with self.in_dir(root):
                        if operator_change:
                            with self.assertRaises(harness.HarnessError):
                                harness.rollback_adoption(root)
                        else:
                            harness.rollback_adoption(root)
                    if operator_change:
                        if operator_change == "bytes":
                            self.assertEqual(target.read_text(encoding="utf-8"),
                                             "operator replacement\n")
                        else:
                            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                        self.assertNotEqual(harness.load_json(journal_path)["lifecycle_state"],
                                            "ADOPTION_ROLLED_BACK")
                    else:
                        self.assertFalse(target.exists())
                finally:
                    temp.cleanup()

    def test_adoption_rollback_operator_edit_preserves_file_and_blocks_success(self):
        temp, root = self.git_project()
        try:
            original = b"operator ignore bytes\n"
            target = root / ".gitignore"
            target.write_bytes(original)
            journal_path = self.interrupted_adoption(root)
            target.write_bytes(b"operator changed this file\n")
            with self.in_dir(root), self.assertRaises(harness.HarnessError):
                harness.rollback_adoption(root)
            self.assertEqual(target.read_bytes(), b"operator changed this file\n")
            self.assertEqual(harness.load_json(journal_path)["lifecycle_state"], "HUMAN_CHECKPOINT")
        finally:
            temp.cleanup()

    def test_recoverable_install_failure_rolls_back_automatically(self):
        temp, root = self.git_project()
        try:
            (root / ".gitignore").write_text("original\n", encoding="utf-8")
            report, analyst, auditor, _final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            original_write = harness.atomic_runtime_write
            failed = False

            def fail_once(path, data, mode=0o644):
                nonlocal failed
                if pathlib.Path(path) == root / ".harness/config.toml" and not failed:
                    failed = True
                    raise harness.HarnessError("simulated recoverable install failure")
                return original_write(path, data, mode)

            with self.in_dir(root), mock.patch.object(harness, "atomic_runtime_write", side_effect=fail_once), \
                    self.assertRaisesRegex(harness.HarnessError, "simulated recoverable"):
                harness.adopt_project(self.adoption_args(report, analyst, auditor, approved=True,
                                                         approved_plan_hash=plan["plan_sha256"]))
            journal = harness.load_json(harness.adoption_control(root)[0])
            self.assertEqual(journal["lifecycle_state"], "ADOPTION_ROLLED_BACK")
            self.assertEqual((root / ".gitignore").read_text(), "original\n")
            self.assertFalse((root / ".harness").exists())
        finally:
            temp.cleanup()

    def test_adoption_rollback_rejects_head_change_and_damaged_backup(self):
        temp, root = self.git_project()
        try:
            self.interrupted_adoption(root)
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "Concurrent commit"], cwd=root, check=True)
            with self.in_dir(root), self.assertRaisesRegex(harness.HarnessError, "HEAD changed"):
                harness.rollback_adoption(root)
        finally:
            temp.cleanup()
        temp, root = self.git_project()
        try:
            (root / ".gitignore").write_text("original\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            journal = harness.load_json(journal_path)
            record = next(item for item in journal["backups"] if item["path"] == ".gitignore")
            pathlib.Path(record["backup_path"]).unlink()
            with self.in_dir(root), self.assertRaisesRegex(harness.HarnessError, "backup is missing"):
                harness.rollback_adoption(root)
        finally:
            temp.cleanup()
        temp, root = self.git_project()
        try:
            (root / ".gitignore").write_text("original\n", encoding="utf-8")
            journal_path = self.interrupted_adoption(root)
            record = next(item for item in harness.load_json(journal_path)["backups"]
                          if item["path"] == ".gitignore")
            pathlib.Path(record["backup_path"]).write_text("damaged backup\n", encoding="utf-8")
            with self.in_dir(root), self.assertRaisesRegex(harness.HarnessError, "backup digest mismatch"):
                harness.rollback_adoption(root)
        finally:
            temp.cleanup()
    def test_report_config_path_context_and_gate_bindings_fail_closed(self):
        temp, root = self.git_project()
        try:
            report, analyst, auditor, _final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            args = self.adoption_args(report, analyst, auditor, approved=True,
                                      approved_plan_hash=plan["plan_sha256"])
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(args)
            p = harness.paths(root)
            journal_path, _ = harness.adoption_control(root)
            journal = harness.load_json(journal_path)
            lifecycle = harness.load_json(root / ".harness/lifecycle.json")
            harness.verify_adoption_bindings(root, p, lifecycle, journal)

            targets = [root / "HARNESS_ADOPTION_REPORT.md", p["config"], root / "README.md"]
            for target in targets:
                original = target.read_bytes()
                target.write_bytes(original + b"\nchanged\n")
                with self.subTest(target=target.name), self.assertRaises(harness.HarnessError):
                    harness.verify_adoption_bindings(root, p, lifecycle, journal)
                target.write_bytes(original)

            for field in ("protected_paths", "context_files", "quality_gates"):
                changed = json.loads(json.dumps(lifecycle))
                changed[field] = []
                with self.subTest(binding=field), self.assertRaises(harness.HarnessError):
                    harness.verify_adoption_bindings(root, p, changed, journal)
            changed = json.loads(json.dumps(journal))
            changed["plan_core"]["bindings"]["analyst_report"] = "0" * 64
            with self.assertRaises(harness.HarnessError):
                harness.verify_adoption_bindings(root, p, lifecycle, changed)
        finally:
            temp.cleanup()

    def test_contradictory_final_pass_never_reaches_harness_ready(self):
        temp, root = self.git_project()
        try:
            report, analyst, auditor, final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            args = self.adoption_args(report, analyst, auditor, approved=True,
                                      approved_plan_hash=plan["plan_sha256"])
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(args)
            p = harness.paths(root)
            for path in (p["state"], p["trusted_state"]):
                state = json.loads(path.read_text())
                state["workflow_state"] = "STAGE_COMPLETED"
                path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            value = json.loads(final.read_text())
            value["findings"] = ["critical language defect"]
            final.write_text(json.dumps(value), encoding="utf-8")
            with self.in_dir(root), self.assertRaises(harness.HarnessError):
                harness.activate_project(argparse.Namespace(final_review=str(final),
                                                            operator_input="", operator_language="en"))
            self.assertEqual(harness.load_json(root / ".harness/lifecycle.json")["lifecycle_state"],
                             "ADOPTION_VALIDATING")
        finally:
            temp.cleanup()

    def test_activation_faults_never_publish_before_durable_commit_and_resume(self):
        phases = tuple(phase for phase in (*harness.ADOPTION_JOURNAL_PHASES,
                                           *harness.ACTIVATION_WRITE_PHASES)
                       if phase.startswith(("activation_", "canonical_", "lifecycle_")))
        for phase in phases:
            temp, root = self.git_project()
            try:
                report, analyst, auditor, final = self.write_reports(root)
                plan = self.approved_plan(root, report, analyst, auditor)
                with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                    harness.adopt_project(self.adoption_args(
                        report, analyst, auditor, approved=True,
                        approved_plan_hash=plan["plan_sha256"]))
                p = harness.paths(root)
                for path in (p["state"], p["trusted_state"]):
                    value = json.loads(path.read_text())
                    value["workflow_state"] = "STAGE_COMPLETED"
                    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                args = argparse.Namespace(final_review=str(final), operator_input="", operator_language="en")
                with self.in_dir(root), mock.patch.dict(
                        os.environ, {"HARNESS_ACTIVATION_FAULT_AFTER": phase}), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(harness.HarnessError):
                    harness.activate_project(args)
                canonical = harness.load_json(p["trusted"] / "lifecycle.json")
                journal = harness.load_json(harness.adoption_control(root)[0])
                if canonical["lifecycle_state"] == "HARNESS_READY":
                    self.assertEqual(journal["lifecycle_state"], "ACTIVATION_COMMITTED")
                else:
                    self.assertNotEqual(canonical["lifecycle_state"], "HARNESS_READY")
                with self.in_dir(root), mock.patch.dict(os.environ, {}, clear=False), \
                        contextlib.redirect_stdout(io.StringIO()):
                    os.environ.pop("HARNESS_ACTIVATION_FAULT_AFTER", None)
                    harness.activate_project(argparse.Namespace(
                        final_review=None, operator_input="", operator_language="en"))
                self.assertEqual(harness.load_json(p["trusted"] / "lifecycle.json")["lifecycle_state"],
                                 "HARNESS_READY")
                self.assertEqual(harness.load_json(p["base"] / "lifecycle.json"),
                                 harness.load_json(p["trusted"] / "lifecycle.json"))
            finally:
                temp.cleanup()

    def test_mirror_readiness_is_not_authoritative(self):
        temp, root = self.git_project()
        try:
            report, analyst, auditor, final = self.write_reports(root)
            plan = self.approved_plan(root, report, analyst, auditor)
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.adopt_project(self.adoption_args(report, analyst, auditor, approved=True,
                                                         approved_plan_hash=plan["plan_sha256"]))
            p = harness.paths(root)
            mirror = harness.load_json(p["base"] / "lifecycle.json")
            mirror["lifecycle_state"] = "HARNESS_READY"
            p["base"].joinpath("lifecycle.json").write_text(
                json.dumps(mirror, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.assertEqual(harness.lifecycle_detection(root)["lifecycle_state"], "ADOPTION_VALIDATING")
            for path in (p["state"], p["trusted_state"]):
                value = json.loads(path.read_text())
                value["workflow_state"] = "STAGE_COMPLETED"
                path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.activate_project(argparse.Namespace(final_review=str(final),
                                                            operator_input="", operator_language="en"))
            self.assertEqual(harness.load_json(p["trusted"] / "lifecycle.json")["lifecycle_state"],
                             "HARNESS_READY")
        finally:
            temp.cleanup()
    def test_hermes_configuration_script_backs_up_and_disables_automatic_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            config = root / "config.yaml"
            config.write_text("model: test\nmemory:\n  memory_enabled: true\ncurator:\n  enabled: true\n", encoding="utf-8")
            subprocess.run([str(ROOT / "scripts/configure-hermes")], check=True,
                           env={**os.environ, "HERMES_DEPLOY_HOME": str(root)},
                           stdout=subprocess.DEVNULL)
            value = yaml.safe_load(config.read_text(encoding="utf-8"))
            self.assertFalse(value["memory"]["memory_enabled"])
            self.assertFalse(value["memory"]["user_profile_enabled"])
            self.assertEqual(value["memory"]["nudge_interval"], 0)
            self.assertEqual(value["skills"]["creation_nudge_interval"], 0)
            self.assertTrue(value["skills"]["write_approval"])
            self.assertFalse(value["curator"]["enabled"])
            self.assertEqual(len(list(root.glob("config.yaml.bak.lifecycle-*"))), 1)


if __name__ == "__main__":
    unittest.main()
