from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy_transaction.py"
import importlib.util

deploy_spec = importlib.util.spec_from_file_location("deploy_transaction", DEPLOY)
deploy_module = importlib.util.module_from_spec(deploy_spec)
deploy_spec.loader.exec_module(deploy_module)
PHASES = deploy_module.PHASES


def snapshot(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_file() or path.is_symlink():
        return ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return ("dir", {str(item.relative_to(path)): (item.read_bytes(), item.stat().st_mode & 0o777)
                    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file())})


class DeploymentTests(unittest.TestCase):
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
            "model: test\nmemory:\n  memory_enabled: true\nskills:\n  creation_nudge_interval: 10\n",
            encoding="utf-8")
        (hermes / "codex-harness-deployment-manifest.json").write_text("old manifest\n", encoding="utf-8")
        env = {**os.environ, "HERMES_DEPLOY_HOME": str(hermes),
               "HARNESS_BIN_DIR": str(bin_dir), "GROUP_PROJECT_ROOT": str(group)}
        targets = (
            hermes / "skills/software-development/codex-harness",
            hermes / "skills/software-development/project-lifecycle-harness",
            hermes / "plugins/codex-harness-context", bin_dir / "harness",
            group / ".harness/config.toml", hermes / "config.yaml",
            hermes / "codex-harness-deployment-manifest.json",
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
        for phase in PHASES:
            temp, _base, payload, hermes, _bin_dir, _group, env, targets = self.fixture()
            try:
                before = [snapshot(path) for path in targets]
                result = self.deploy(payload, env, fault=phase)
                self.assertNotEqual(result.returncode, 0, phase)
                self.assertEqual(before, [snapshot(path) for path in targets], phase)
                self.assertFalse((hermes / "deployment-backups").exists(), phase)
            finally:
                temp.cleanup()

    def test_successful_temporary_deployment_excludes_generated_files_and_records_manifest(self):
        temp, _base, payload, hermes, bin_dir, group, env, _targets = self.fixture()
        try:
            result = self.deploy(payload, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = hermes / "skills/software-development/project-lifecycle-harness"
            self.assertTrue((installed / "schemas/final-review.schema.json").is_file())
            self.assertFalse(any(path.name == "__pycache__" or path.suffix == ".pyc"
                                 for path in hermes.rglob("*")))
            self.assertEqual((bin_dir / "harness").read_bytes(), (payload / "bin/harness").read_bytes())
            self.assertEqual((group / ".harness/config.toml").read_bytes(),
                             (payload / "projects/Group/config.toml").read_bytes())
            manifest = hermes / "codex-harness-deployment-manifest.json"
            self.assertIn("deployment_manifest_sha256=", result.stdout)
            self.assertEqual(json.loads(manifest.read_text())["frozen_commit"], "test-frozen-commit")
            self.assertEqual(len(list((hermes / "deployment-backups").iterdir())), 1)
        finally:
            temp.cleanup()

    def test_doctor_payload_mutation_is_detected_and_rolled_back(self):
        temp, _base, payload, hermes, _bin_dir, _group, env, targets = self.fixture()
        try:
            before = [snapshot(path) for path in targets]
            installed_skill = hermes / "skills/software-development/codex-harness"
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
            launcher = bin_dir / "harness"
            run_env = {**env, "HERMES_HOME": str(hermes)}
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
