from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "harness"
loader = importlib.machinery.SourceFileLoader("codex_harness", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
harness = importlib.util.module_from_spec(spec)
loader.exec_module(harness)


class ChunkPipe:
    def __init__(self, chunks: list[str]):
        self.chunks = iter(chunks)

    def read(self, _size: int) -> str:
        return next(self.chunks, "")


class TrackingLog(io.StringIO):
    def __init__(self):
        super().__init__()
        self.writes: list[str] = []

    def write(self, value: str) -> int:
        self.writes.append(value)
        return super().write(value)


class HarnessTests(unittest.TestCase):
    def make_project(self) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path]:
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
        (root / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
        with self.in_dir(root):
            harness.init_project(argparse.Namespace())
        return temp, root

    @contextlib.contextmanager
    def in_dir(self, path: pathlib.Path):
        old = pathlib.Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old)

    def test_hardlink_log_attack_replaces_name_without_truncating_victim(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            victim = directory / "victim"
            victim.write_text("KEEP", encoding="utf-8")
            log = directory / "worker.log"
            os.link(victim, log)
            with harness.secure_log_file(log) as handle:
                handle.write("new")
            self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP")
            self.assertEqual(log.read_text(encoding="utf-8"), "new")
            self.assertNotEqual(victim.stat().st_ino, log.stat().st_ino)
            self.assertEqual(log.stat().st_nlink, 1)

    def test_artifact_readers_reject_hardlink_fifo_socket_and_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            regular = directory / "value.json"
            regular.write_text("{}", encoding="utf-8")
            hardlink = directory / "hard.json"
            os.link(regular, hardlink)
            fifo = directory / "fifo.json"
            os.mkfifo(fifo)
            symlink = directory / "link.json"
            symlink.symlink_to(regular.name)
            sock_path = directory / "socket.json"
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(sock_path))
            try:
                for path in (hardlink, fifo, symlink, sock_path):
                    with self.subTest(path=path.name):
                        with self.assertRaises(harness.HarnessError):
                            harness.load_json(path)
                        with self.assertRaises(harness.HarnessError):
                            harness.read_log_tail(path, 100)
            finally:
                sock.close()

    def test_symlink_fingerprint_records_target_without_following_it(self):
        temp, root = self.make_project()
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        outside_path = pathlib.Path(outside.name)
        try:
            outside_path.write_text("one", encoding="utf-8")
            (root / "linked").symlink_to(outside_path)
            first = harness.worktree_fingerprint(root)
            outside_path.write_text("two", encoding="utf-8")
            second = harness.worktree_fingerprint(root)
            self.assertEqual(first, second)
            (root / "linked").unlink()
            (root / "linked").symlink_to(str(outside_path) + "-different")
            self.assertNotEqual(first, harness.worktree_fingerprint(root))
        finally:
            outside_path.unlink(missing_ok=True)
            temp.cleanup()

    def test_streaming_redaction_handles_long_split_tokens_and_families(self):
        secrets = [
            "Bearer " + "B" * 6000,
            "sk-proj-" + "O" * 64,
            "github_pat_" + "G" * 40,
            "AKIA" + "A" * 16,
            "AIza" + "I" * 32,
            "glpat-" + "L" * 32,
            "xoxb-" + "1" * 32,
            "sk_live_" + "S" * 32,
            "sig=" + "Z" * 64,
            "AccountKey=" + "K" * 64,
            '"token":"plain-secret-value"',
            '"password": "hunter2"',
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            '"authorization":"Bearer quoted-json-secret"',
        ]
        text = "\n".join(secrets) + "\nvisible\n"
        chunks = [text[index:index + 37] for index in range(0, len(text), 37)]
        log = TrackingLog()
        harness.stream_redacted(ChunkPipe(chunks), log)
        output = log.getvalue()
        self.assertIn("visible", output)
        self.assertGreaterEqual(output.count("[REDACTED]"), len(secrets))
        for value in secrets:
            self.assertNotIn(value[-12:], output)

    def test_unterminated_possible_secret_is_not_written_before_eof(self):
        log = TrackingLog()
        harness.stream_redacted(ChunkPipe(["Bearer ", "A" * 9000]), log)
        self.assertEqual(log.writes, ["[REDACTED]"])

    def write_active_state(self, root: pathlib.Path, *, generation: str,
                           owner: dict, role: str = "implementer", attempt: int = 1):
        p = harness.paths(root)
        state = harness.load_state(p)
        state.update({"workflow_state": "IMPLEMENTING" if role == "implementer" else "REVIEWING",
                      "stage_id": "S1", "slice_id": "S1.1", "attempt": attempt,
                      "owner": owner,
                      "worker": {"role": role, "pid": None, "process_identity": None,
                                 "generation": generation, "stage_id": "S1", "slice_id": "S1.1",
                                 "attempt": attempt,
                                 "expected_output": str(p["runtime"] / f"{role}-{generation}.json")}})
        harness.save_state(p, state)
        return p, state

    def test_recover_rejects_stale_checkpoint(self):
        temp, root = self.make_project()
        try:
            dead_owner = {"pid": 99999999, "identity": {"start_time": "0"}, "generation": "new"}
            p, state = self.write_active_state(root, generation="new", owner=dead_owner)
            checkpoint = {"stage_id": "S1", "slice_id": "S1.1", "attempt": 0,
                          "worker_role": "reviewer", "generation": "old",
                          "owner_pid": dead_owner["pid"], "owner_identity": dead_owner["identity"],
                          "expected_output_file": str(p["runtime"] / "reviewer-old.json")}
            harness.atomic_json(p["trusted_checkpoint"], checkpoint)
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.recover(argparse.Namespace(retry=False, ack_human=False))
            self.assertEqual(harness.load_state(p)["workflow_state"], "BLOCKED")
        finally:
            temp.cleanup()

    def test_recover_does_not_take_over_live_owner(self):
        temp, root = self.make_project()
        try:
            generation = "live"
            owner = {"pid": os.getpid(), "identity": harness.process_identity(os.getpid()),
                     "generation": generation}
            p, state = self.write_active_state(root, generation=generation, owner=owner)
            checkpoint = {"stage_id": "S1", "slice_id": "S1.1", "attempt": 1,
                          "worker_role": "implementer", "generation": generation,
                          "owner_pid": owner["pid"], "owner_identity": owner["identity"],
                          "process_id": None, "process_identity": None,
                          "expected_output_file": state["worker"]["expected_output"]}
            harness.atomic_json(p["trusted_checkpoint"], checkpoint)
            output = io.StringIO()
            with self.in_dir(root), contextlib.redirect_stdout(output):
                harness.recover(argparse.Namespace(retry=False, ack_human=False))
            self.assertIn("recover running", output.getvalue())
            self.assertEqual(harness.load_state(p)["workflow_state"], "IMPLEMENTING")
        finally:
            temp.cleanup()

    def test_duplicate_worker_prevention(self):
        temp, root = self.make_project()
        try:
            p = harness.paths(root)
            state = harness.load_state(p)
            state.update({"workflow_state": "SLICE_READY", "stage_id": "S1", "slice_id": "S1.1",
                          "owner": {"pid": os.getpid(), "identity": harness.process_identity(os.getpid()),
                                    "generation": "live"}})
            harness.save_state(p, state)
            with self.in_dir(root), self.assertRaisesRegex(harness.HarnessError, "live Harness owner"):
                harness.run_worker(argparse.Namespace(dry_run=False), "implementer")
        finally:
            temp.cleanup()

    def test_gate_timeout_reclaims_descendant_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            log = root / "gate.log"
            code = ("import subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                    "print(p.pid,flush=True); time.sleep(60)")
            env = harness.gate_environment(root / "home")
            rc = harness.run_logged([sys.executable, "-c", code], root, log, 1, env)
            self.assertEqual(rc, 124)
            child_pid = int(log.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 3
            while pathlib.Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(pathlib.Path(f"/proc/{child_pid}").exists())

    def test_runtime_overwrite_cannot_modify_hardlinked_control_plane(self):
        temp, root = self.make_project()
        try:
            p = harness.paths(root)
            trusted = p["trusted_implementation"]
            harness.atomic_json(trusted, {"trusted": True})
            runtime = p["runtime"] / "implementation.json"
            os.link(trusted, runtime)
            harness.atomic_runtime_json(runtime, {"trusted": False})
            self.assertEqual(harness.load_json(trusted), {"trusted": True})
            self.assertEqual(harness.load_json(runtime), {"trusted": False})
        finally:
            temp.cleanup()

    def test_worker_timeout_transitions_to_blocked(self):
        temp, root = self.make_project()
        try:
            fake = root / "fake-codex"
            fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n", encoding="utf-8")
            fake.chmod(0o755)
            p = harness.paths(root)
            config = p["config"].read_text(encoding="utf-8").replace(
                "worker_timeout_seconds = 3600", "worker_timeout_seconds = 1", 1)
            harness.atomic_write(p["config"], config)
            harness.atomic_write(p["trusted_config"], config, mode=0o600)
            state = harness.load_state(p)
            state.update({"workflow_state": "SLICE_READY", "stage_id": "S1", "slice_id": "S1.1",
                          "owner": None})
            harness.save_state(p, state)
            with self.in_dir(root), mock.patch.dict(os.environ, {"HARNESS_CODEX_BIN": str(fake)}), \
                    self.assertRaisesRegex(harness.HarnessError, "timeout"):
                harness.run_worker(argparse.Namespace(dry_run=False), "implementer")
            self.assertEqual(harness.load_state(p)["workflow_state"], "BLOCKED")
        finally:
            temp.cleanup()

    def test_plan_file_uses_one_open_fd_and_rejects_symlinks(self):
        temp, root = self.make_project()
        try:
            plan = root / "plan.md"
            replacement = root / "replacement.md"
            plan.write_text("ORIGINAL", encoding="utf-8")
            replacement.write_text("REPLACEMENT", encoding="utf-8")
            original_validate = harness.validate_single_link_regular

            def swap_after_validation(fd, label, require_owner=True):
                result = original_validate(fd, label, require_owner)
                os.replace(replacement, plan)
                return result

            with mock.patch.object(harness, "validate_single_link_regular", side_effect=swap_after_validation):
                self.assertEqual(harness.read_approved_plan(root, "plan.md"), "ORIGINAL")
            target = root / "target.md"
            target.write_text("TARGET", encoding="utf-8")
            link = root / "link.md"
            link.symlink_to(target.name)
            with self.assertRaises(harness.HarnessError):
                harness.read_approved_plan(root, "link.md")
        finally:
            temp.cleanup()

    def test_status_is_byte_and_mtime_read_only(self):
        temp, root = self.make_project()
        try:
            watched = [path for base in (root / ".harness", root / ".git" / "harness-control")
                       for path in base.rglob("*") if path.is_file()]
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in watched}
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.status(argparse.Namespace(json=True))
            after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in watched}
            self.assertEqual(before, after)
        finally:
            temp.cleanup()

    def test_gate_environment_is_allowlisted_and_credential_free(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/secret/codex", "HERMES_HOME": "/secret/hermes",
                                          "OPENAI_API_KEY": "secret", "HTTPS_PROXY": "http://credential",
                                          "PATH": os.environ.get("PATH", "")}, clear=True):
            env = harness.gate_environment(pathlib.Path("/tmp/gate-home"))
        self.assertEqual(env["HOME"], "/tmp/gate-home")
        for name in ("CODEX_HOME", "HERMES_HOME", "OPENAI_API_KEY", "HTTPS_PROXY"):
            self.assertNotIn(name, env)

    def test_state_cas_rejects_stale_writer(self):
        temp, root = self.make_project()
        try:
            p = harness.paths(root)
            first = harness.load_state(p)
            stale = dict(first)
            first["stage_title"] = "first"
            harness.save_state(p, first)
            stale["stage_title"] = "stale"
            with self.assertRaisesRegex(harness.HarnessError, "CAS conflict"):
                harness.save_state(p, stale)
        finally:
            temp.cleanup()

    def test_doctor_accepts_previous_compatible_harness_version(self):
        temp, root = self.make_project()
        try:
            p = harness.paths(root)
            for key in ("config", "trusted_config"):
                config = p[key].read_text(encoding="utf-8").replace(
                    f'harness_version = "{harness.HARNESS_VERSION}"',
                    'harness_version = "0.2.0"')
                harness.atomic_write(p[key], config, mode=0o600 if key == "trusted_config" else 0o644)
            fake_checks = {name: (True, "ok") for name in ("git", "python", "hermes", "codex")}

            def version(name, _args):
                key = "python" if name == os.sys.executable else name
                return (True, "Hermes Agent v0.19.1") if key == "hermes" else fake_checks[key]

            output = io.StringIO()
            with self.in_dir(root), contextlib.redirect_stdout(output), \
                    mock.patch.object(harness, "executable_version", side_effect=version), \
                    mock.patch.object(harness, "skill_root", return_value=ROOT / "skill"), \
                    mock.patch("importlib.metadata.version", return_value="0.19.1"):
                harness.doctor(argparse.Namespace(capabilities=False))
            self.assertIn('"project_config":true', output.getvalue())
        finally:
            temp.cleanup()

    def test_complete_compatible_020_stage_slice_workflow(self):
        temp, root = self.make_project()
        try:
            p = harness.paths(root)
            config = p["config"].read_text(encoding="utf-8")
            config = config.replace(f'harness_version = "{harness.HARNESS_VERSION}"',
                                    'harness_version = "0.2.0"')
            config = config.replace("stage = []", 'stage = [["git", "diff", "--check"]]')
            harness.atomic_write(p["config"], config)
            plan = root / "baseline-plan.md"
            plan.write_text(
                "# Baseline Validation\n\nVerify the complete compatible Stage and Slice workflow without product changes.\n",
                encoding="utf-8")
            fake = root / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "schema = pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1]).name\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
                "if schema == 'implementation.schema.json':\n"
                "    value = {'status':'completed','summary':'No product changes required.',"
                "'changed_files':[],'tests':['fixture'],'blockers':[]}\n"
                "else:\n"
                "    value = {'verdict':'approved','summary':'Baseline is valid.',"
                "'findings':[],'checks_reviewed':['stage gate']}\n"
                "output.write_text(json.dumps(value), encoding='utf-8')\n",
                encoding="utf-8")
            fake.chmod(0o755)
            with self.in_dir(root), contextlib.redirect_stdout(io.StringIO()):
                harness.start_stage(argparse.Namespace(stage="S1", title="Compatibility Baseline",
                                                       slice="S1.1", plan_file=str(plan)))
            env = {**os.environ, "HARNESS_CODEX_BIN": str(fake)}
            for argv in (("run-implementer",), ("run-gates", "--level", "stage"),
                         ("run-reviewer",), ("approve-slice", "--complete-stage")):
                result = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=root, env=env,
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(harness.load_state(p)["workflow_state"], "STAGE_COMPLETED")
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
