from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_launcher_reports_runtime_version(self) -> None:
        result = subprocess.run([str(ROOT / "bin/codex-harness"), "--version"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2.0.0")

    def test_installer_dry_run_has_only_thin_runtime_targets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="thin-install-") as raw:
            root = Path(raw)
            result = subprocess.run(
                [str(ROOT / "scripts/install-codex"), "--dry-run",
                 "--skills-dir", str(root / "skills"), "--bin-dir", str(root / "bin")],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        targets = [Path(item["target"]).name for item in value["operations"]]
        self.assertEqual(targets, ["codex-harness", "project-lifecycle-harness",
                                   "codex-harness"])


if __name__ == "__main__":
    unittest.main()
