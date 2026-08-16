from __future__ import annotations

import json
import fcntl
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "skill/scripts/codex_runtime.py"


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="thin-harness-")
        self.root = Path(self.temp.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("do the bounded task\n", encoding="utf-8")
        self.fake = self.root / "codex"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_codex(self, body: str) -> None:
        self.fake.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        self.fake.chmod(0o755)

    def invoke(self, *extra: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(RUNTIME), *extra, "--codex", str(self.fake)],
            cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )

    def metadata(self) -> dict[str, object]:
        paths = sorted((self.root / ".codex-harness/runs").glob("*/metadata.json"))
        return json.loads(paths[-1].read_text(encoding="utf-8"))

    def test_success_captures_thread_usage_and_artifacts(self) -> None:
        self.fake_codex("""
            import json, pathlib, sys
            args = sys.argv[1:]
            output = pathlib.Path(args[args.index('--output-last-message') + 1])
            output.write_text('done\\n')
            print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-123'}))
            print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7}}))
        """)
        result = self.invoke("run", "--workspace", str(self.root), "--prompt-file", str(self.prompt),
                             "--sandbox", "read-only", "--reasoning-effort", "low")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.metadata()
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["thread_id"], "thread-123")
        self.assertEqual(value["usage"], {"input_tokens": 7})
        run = self.root / ".codex-harness/runs" / str(value["run_id"])
        self.assertEqual((run / "final.md").read_text(), "done\n")
        self.assertTrue((run / "events.jsonl").is_file())

    def test_nonzero_is_failed_and_preserves_stderr(self) -> None:
        self.fake_codex("""
            import sys
            print('specific failure', file=sys.stderr)
            raise SystemExit(9)
        """)
        result = self.invoke("run", "--workspace", str(self.root), "--prompt-file", str(self.prompt))
        self.assertEqual(result.returncode, 9)
        value = self.metadata()
        self.assertEqual((value["state"], value["exit_code"]), ("failed", 9))
        run = self.root / ".codex-harness/runs" / str(value["run_id"])
        self.assertIn("specific failure", (run / "stderr.log").read_text())

    def test_timeout_is_terminal_and_classified(self) -> None:
        self.fake_codex("""
            import time
            time.sleep(30)
        """)
        result = self.invoke("run", "--workspace", str(self.root), "--prompt-file", str(self.prompt),
                             "--timeout", "0.05")
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.metadata()["state"], "timed_out")

    def test_resume_uses_exact_saved_thread(self) -> None:
        self.fake_codex("""
            import json, pathlib, sys
            args = sys.argv[1:]
            capture = pathlib.Path(sys.argv[0]).with_name('argv.json')
            capture.write_text(json.dumps(args))
            output = pathlib.Path(args[args.index('--output-last-message') + 1])
            output.write_text('ok')
            print(json.dumps({'type': 'thread.started', 'thread_id': 'exact-thread'}))
        """)
        first = self.invoke("run", "--workspace", str(self.root), "--prompt-file", str(self.prompt))
        self.assertEqual(first.returncode, 0)
        run_id = str(self.metadata()["run_id"])
        second = self.invoke("resume", "--workspace", str(self.root), "--prompt-file", str(self.prompt),
                             "--run-id", run_id)
        self.assertEqual(second.returncode, 0, second.stderr)
        argv = json.loads((self.root / "argv.json").read_text())
        self.assertEqual(argv[argv.index("resume") + 1], "exact-thread")
        self.assertEqual(self.metadata()["parent_run_id"], run_id)

    def test_recovery_marks_abandoned_running_record_interrupted(self) -> None:
        abandoned = self.root / ".codex-harness/runs/20000101T000000-deadbeef/metadata.json"
        abandoned.parent.mkdir(parents=True)
        abandoned.write_text(json.dumps({"state": "running"}), encoding="utf-8")
        self.fake_codex("""
            import json
            print(json.dumps({'type': 'thread.started', 'thread_id': 'new'}))
        """)
        result = self.invoke("run", "--workspace", str(self.root), "--prompt-file", str(self.prompt))
        self.assertEqual(result.returncode, 0)
        recovered = json.loads(abandoned.read_text())
        self.assertEqual(recovered["state"], "interrupted")

    def test_wip_limit_rejects_a_second_active_run(self) -> None:
        lock_path = self.root / ".codex-harness/run.lock"
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fake_codex("raise SystemExit(0)\n")
            result = self.invoke("run", "--workspace", str(self.root),
                                 "--prompt-file", str(self.prompt))
        self.assertEqual(result.returncode, 2)
        self.assertIn("WIP limit is 1", result.stderr)

    def test_ephemeral_run_cannot_be_resumed(self) -> None:
        self.fake_codex("""
            import json
            print(json.dumps({'type': 'thread.started', 'thread_id': 'not-persisted'}))
        """)
        first = self.invoke("run", "--workspace", str(self.root), "--prompt-file", str(self.prompt),
                            "--ephemeral")
        self.assertEqual(first.returncode, 0)
        run_id = str(self.metadata()["run_id"])
        second = self.invoke("resume", "--workspace", str(self.root),
                             "--prompt-file", str(self.prompt), "--run-id", run_id)
        self.assertEqual(second.returncode, 2)
        self.assertIn("ephemeral", second.stderr)


if __name__ == "__main__":
    unittest.main()
