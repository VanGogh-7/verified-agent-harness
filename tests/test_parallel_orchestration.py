from __future__ import annotations

import compileall
import argparse
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skill"


def load_harness() -> dict[str, object]:
    namespace: dict[str, object] = {
        "__name__": "parallel_harness_under_test",
        "__file__": str(SKILL_ROOT / "scripts" / "harness"),
    }
    for name in ("harness_core.py", "harness_parallel.py", "harness_commands.py"):
        path = SKILL_ROOT / "scripts" / name
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace, namespace)
    return namespace


H = load_harness()
HarnessError = H["HarnessError"]
SCHEMA = SKILL_ROOT / "references" / "contracts" / "parallel-plan.schema.json"


def task(task_id: str, *, dependencies: list[str], path: str) -> dict[str, object]:
    return {
        "id": task_id, "title": task_id, "depends_on": dependencies,
        "write_paths": [path], "acceptance": [f"{task_id} passes."],
        "contract_ids": ["api"], "resource_class": "light",
    }


def plan() -> dict[str, object]:
    return {
        "schema_version": "1.0", "decision": "parallel",
        "rationale": "Independent consumers follow one contract owner.", "base_sha": "a" * 40,
        "shared_contracts": [{"id": "api", "owner": "contract", "paths": ["src/api.py"],
                              "acceptance": ["API is frozen."]}],
        "tasks": [task("contract", dependencies=[], path="src/api.py"),
                  task("left", dependencies=["contract"], path="src/left.py"),
                  task("right", dependencies=["contract"], path="src/right.py")],
        "integration": {"order": ["contract", "left", "right"],
                        "gates": [["python3", "-m", "unittest"]]},
    }


class ParallelOrchestrationTests(unittest.TestCase):
    def test_contract_and_wave_scheduler(self) -> None:
        value = plan()
        H["validate_parallel_plan"](value, SCHEMA)
        self.assertEqual(H["parallel_waves"](value), [["contract"], ["left", "right"]])

    def test_plan_rejects_cycles_overlap_and_invalid_contract_owner(self) -> None:
        value = plan()
        value["tasks"][1]["depends_on"] = ["right"]
        value["tasks"][2]["depends_on"] = ["left"]
        with self.assertRaisesRegex(HarnessError, "cycle"):
            H["validate_parallel_plan"](value, SCHEMA)
        value = plan()
        value["tasks"][2]["write_paths"] = ["src/left.py"]
        with self.assertRaisesRegex(HarnessError, "overlapping write path"):
            H["validate_parallel_plan"](value, SCHEMA)
        value = plan()
        value["shared_contracts"][0]["owner"] = "missing"
        with self.assertRaisesRegex(HarnessError, "contract owner"):
            H["validate_parallel_plan"](value, SCHEMA)
        for unsafe in ("src//left.py", "src/./left.py", "src\\left.py", "src/line\nbreak.py"):
            value = plan()
            value["tasks"][1]["write_paths"] = [unsafe]
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(HarnessError, "normalized"):
                H["validate_parallel_plan"](value, SCHEMA)

    def test_memory_policy_uses_hysteresis(self) -> None:
        gib = 1024 ** 3
        action = H["parallel_memory_action"]
        self.assertEqual(action(total_bytes=16*gib, available_bytes=gib,
                                swap_free_bytes=256*1024**2, psi_some_avg10=12,
                                psi_full_avg10=2, paused_count=0), "pause_heavy_first")
        self.assertEqual(action(total_bytes=16*gib, available_bytes=2*gib,
                                swap_free_bytes=2*gib, psi_some_avg10=6,
                                psi_full_avg10=.2, paused_count=0), "pause_one")
        self.assertEqual(action(total_bytes=16*gib, available_bytes=4*gib,
                                swap_free_bytes=2*gib, psi_some_avg10=0,
                                psi_full_avg10=0, paused_count=1), "hold")
        self.assertEqual(action(total_bytes=16*gib, available_bytes=6*gib,
                                swap_free_bytes=2*gib, psi_some_avg10=0,
                                psi_full_avg10=0, paused_count=1), "resume_one")

    def test_serial_plan_can_intentionally_serialize_parallel_ready_tasks(self) -> None:
        value = plan()
        value["decision"] = "serial"
        H["validate_parallel_plan"](value, SCHEMA)

    def test_frozen_contract_owner_is_dependency_free_and_contract_only(self) -> None:
        value = plan()
        value["tasks"][0]["depends_on"] = ["left"]
        with self.assertRaisesRegex(HarnessError, "frozen contract owner"):
            H["validate_parallel_plan"](value, SCHEMA)
        value = plan()
        value["tasks"][0]["write_paths"].append("src/extra.py")
        with self.assertRaisesRegex(HarnessError, "frozen contract owner"):
            H["validate_parallel_plan"](value, SCHEMA)

    def test_linux_metrics_and_commands(self) -> None:
        metrics = H["parse_linux_memory_metrics"](
            "MemTotal: 16384000 kB\nMemAvailable: 4194304 kB\nSwapFree: 1048576 kB\n",
            "some avg10=6.25 avg60=1 avg300=.5 total=10\nfull avg10=0.75 avg60=.2 avg300=.1 total=2\n",
        )
        self.assertEqual(metrics["available_bytes"], 4194304 * 1024)
        self.assertEqual(metrics["psi_full_avg10"], .75)
        commands = H["parser"]()._subparsers._group_actions[0].choices
        self.assertIn("validate-parallel-plan", commands)
        self.assertIn("memory-action", commands)

    def test_documented_outer_workflow_and_schema(self) -> None:
        workflow = (SKILL_ROOT / "references" / "parallel-workflow.md").read_text(encoding="utf-8")
        for phrase in ("Hermes-led decomposition", "isolated Git worktree", "shared contract freeze",
                       "one inner Harness validation", "integration candidate",
                       "continuous memory guard", "selectively pause", "Linux PSI",
                       "does not claim to be a distributed scheduler",
                       "changed paths derived from Git", "fall back to serial execution"):
            self.assertIn(phrase, workflow)
        self.assertIn("references/parallel-workflow.md",
                      (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])

    def test_freeze_binds_plan_base_and_contract_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "src/api.py").write_text("def api():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  text=True, capture_output=True).stdout.strip()
            value = plan()
            value["base_sha"] = base
            frozen = H["freeze_parallel_plan"](root, value, SCHEMA, "run-1")
            self.assertEqual(frozen["base_sha"], base)
            self.assertRegex(frozen["plan_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(frozen["contract_set_id"], r"^parallel-contract-v1:[0-9a-f]{64}$")
            self.assertEqual(frozen["contract_paths"], ["src/api.py"])
            changed = plan()
            changed["base_sha"] = base
            changed["rationale"] = "different"
            self.assertNotEqual(H["parallel_plan_digest"](value), H["parallel_plan_digest"](changed))

    def test_lane_validation_uses_git_diff_and_rejects_scope_escape_or_stale_base(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "src").mkdir()
            for name in ("api.py", "left.py", "right.py"):
                (root / f"src/{name}").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  text=True, capture_output=True).stdout.strip()
            (root / "src/left.py").write_text("left\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "left"], cwd=root, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  text=True, capture_output=True).stdout.strip()
            report = H["validate_parallel_lane"](root, base, head, ["src/left.py"])
            self.assertEqual(report["changed_paths"], ["src/left.py"])
            with self.assertRaisesRegex(HarnessError, "outside approved write scope"):
                H["validate_parallel_lane"](root, base, head, ["src/right.py"])
            with self.assertRaisesRegex(HarnessError, "descend"):
                H["validate_parallel_lane"](root, head, base, ["src/left.py"])

    def test_cgroup_freeze_thaw_requires_complete_verified_containment(self) -> None:
        self.assertNotIn("set_worker_cgroup_frozen", H)
        with self.assertRaisesRegex(HarnessError, "complete verified worker containment"):
            H["apply_parallel_memory_action"]("pause_one", [])

    def test_outer_controller_commands_are_exposed(self) -> None:
        commands = H["parser"]()._subparsers._group_actions[0].choices
        for command in ("freeze-parallel-plan", "validate-parallel-lane", "record-parallel-lane",
                        "parallel-integration-state", "register-parallel-worker",
                        "activate-parallel-worker",
                        "memory-guard-step", "memory-guard"):
            self.assertIn(command, commands)

    def test_outer_state_skips_frozen_contract_owner_and_orders_validated_lanes(self) -> None:
        value = plan()
        frozen = {"run_id": "run-1", "base_sha": "a" * 40,
                  "plan_sha256": H["parallel_plan_digest"](value),
                  "contract_set_id": "parallel-contract-v1:" + "b" * 64}
        state = H["initial_parallel_state"](value, frozen)
        self.assertEqual(state["tasks"]["contract"]["status"], "frozen_in_base")
        self.assertEqual(state["integration_order"], ["left", "right"])
        H["record_parallel_lane"](state, value, "right", "c" * 40, ["src/right.py"])
        self.assertEqual(H["parallel_integration_state"](state)["next_task"], "left")
        H["record_parallel_lane"](state, value, "left", "d" * 40, ["src/left.py"])
        ready = H["parallel_integration_state"](state)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["ordered_heads"], ["d" * 40, "c" * 40])
        with self.assertRaisesRegex(HarnessError, "contract owner"):
            H["record_parallel_lane"](state, value, "contract", "e" * 40, ["src/api.py"])

    def test_parallel_control_root_rejects_symlinked_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            common = root / ".git/harness-parallel"
            common.mkdir()
            outside = Path(raw) / "outside"
            outside.mkdir()
            (common / "run-1").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(HarnessError, "symlink"):
                H["parallel_control_root"](root, "run-1")
            real = Path(raw) / "real"
            real.mkdir()
            ancestor = Path(raw) / "linked"
            ancestor.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(HarnessError, "symlink|ancestry"):
                H["open_directory_path"](ancestor)

    def test_canonical_bundle_rejects_tampering_and_is_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "src/api.py").write_text("api\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  text=True, capture_output=True).stdout.strip()
            value = plan(); value["base_sha"] = base
            freeze = H["freeze_parallel_plan"](root, value, SCHEMA, "run-1")
            bundle = H["parallel_bundle"](value, freeze)
            H["validate_parallel_bundle"](root, bundle, SCHEMA)
            bundle["plan"]["tasks"][1]["write_paths"] = ["src/escape.py"]
            bundle["state"]["plan_sha256"] = H["parallel_plan_digest"](bundle["plan"])
            with self.assertRaisesRegex(HarnessError, "frozen"):
                H["validate_parallel_bundle"](root, bundle, SCHEMA)
            bundle = H["parallel_bundle"](value, freeze)
            bundle["freeze"]["contract_digests"]["src/api.py"] = "0" * 64
            with self.assertRaisesRegex(HarnessError, "contract"):
                H["validate_parallel_bundle"](root, bundle, SCHEMA)
        source = (SKILL_ROOT / "scripts/harness_parallel.py").read_text(encoding="utf-8")
        self.assertIn('"run.json"', source)
        self.assertNotIn('control / "plan.json"', source)
        self.assertNotIn('control / "contract-freeze.json"', source)
        self.assertNotIn('control / "state.json"', source)

    def test_memory_guard_repeats_and_falls_back_serial_without_containment(self) -> None:
        metrics = {"total_bytes": 16 * 1024**3, "available_bytes": 512 * 1024**2,
                   "swap_free_bytes": 128 * 1024**2, "psi_some_avg10": 12.0,
                   "psi_full_avg10": 2.0}
        samples = iter([metrics, metrics])
        events = H["run_memory_guard"](
            {"workers": []}, interval=0, max_samples=2,
            sampler=lambda: next(samples), sleeper=lambda _: None,
        )
        self.assertEqual([event["action"] for event in events], ["serial_fallback", "serial_fallback"])
        self.assertFalse(events[-1]["admission_open"])
        healthy = dict(metrics, available_bytes=8 * 1024**3, swap_free_bytes=2 * 1024**3,
                       psi_some_avg10=0.0, psi_full_avg10=0.0)
        event = H["memory_guard_event"](
            {"workers": [{"id": "unsafe", "paused": False}]}, sampler=lambda: healthy)
        self.assertEqual(event["action"], "blocked_uncontained")
        self.assertFalse(event["safe_to_continue"])
        event = H["memory_guard_event"](
            {"workers": [{"id": "bogus", "paused": False,
                          "cgroup": "/sys/fs/cgroup/verified-agent-harness-missing"}]},
            sampler=lambda: healthy)
        self.assertEqual(event["action"], "blocked_uncontained")

    def test_memory_guard_needs_three_recovered_samples_to_resume(self) -> None:
        gib = 1024**3
        healthy = {"total_bytes": 16*gib, "available_bytes": 8*gib,
                   "swap_free_bytes": 2*gib, "psi_some_avg10": 0.0, "psi_full_avg10": 0.0}
        value = {"workers": [{"id": "w", "paused": True, "activated": True,
                              "cgroup": "/fake"}],
                 "guard": {"recovered_samples": 0}}
        with mock.patch.dict(H, {"validate_worker_cgroup": lambda path: Path("/fake"),
                                 "worker_cgroup_controls_ready": lambda path: True,
                                 "apply_parallel_memory_action": lambda action, workers: ["w"]}):
            events = [H["memory_guard_event"](value, sampler=lambda: healthy) for _ in range(3)]
        self.assertEqual([event["action"] for event in events],
                         ["hold_recovery", "hold_recovery", "resume_one"])

    def test_worker_binding_rejects_stale_generation_or_substitution(self) -> None:
        valid = {"id": "left", "run_id": "run-1", "task_id": "left", "generation": 3,
                 "cgroup": "/expected-" + "a" * 32, "cgroup_device": 1,
                 "cgroup_inode": 2, "cgroup_parent_device": 3, "cgroup_parent_inode": 4,
                 "containment_token": "a" * 32, "activated": False,
                 "resource_class": "light", "paused": False}
        state = {"run_id": "run-1", "generation": 3, "workers": {"left": dict(valid)}}
        tasks = {"left": {"resource_class": "light"}}
        fake_path = mock.Mock(); fake_path.stat.return_value = mock.Mock(st_dev=1, st_ino=2)
        fake_path.name = "verified-agent-harness-" + "a" * 32
        fake_path.parent.stat.return_value = mock.Mock(st_dev=3, st_ino=4)
        with mock.patch.dict(H, {"validate_worker_cgroup": lambda path: fake_path,
                                 "worker_cgroup_controls_ready": lambda path: True}):
            H["validate_worker_binding"](state, "left", valid, tasks)
            for changed in ({"generation": 2}, {"cgroup": "/other"}, {"run_id": "old"}):
                with self.assertRaisesRegex(HarnessError, "binding|identity"):
                    H["validate_worker_binding"](state, "left", {**valid, **changed}, tasks)
            for key, changed in (("wrong", {}), ("left", {"id": "right"}),
                                 ("left", {"task_id": "right"}),
                                 ("left", {"resource_class": "heavy"})):
                with self.assertRaisesRegex(HarnessError, "binding"):
                    H["validate_worker_binding"](state, key, {**valid, **changed}, tasks)
            duplicate = {"left": dict(valid),
                         "right": {**valid, "id": "right", "task_id": "right"}}
            with self.assertRaisesRegex(HarnessError, "unique"):
                H["validate_worker_set"]({**state, "workers": duplicate},
                                         {**tasks, "right": {"resource_class": "light"}})

    def test_canonical_state_rejects_forged_guard_and_invalid_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "src").mkdir(); (root / "src/api.py").write_text("api\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            value = plan(); value["base_sha"] = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                text=True, capture_output=True).stdout.strip()
            freeze = H["freeze_parallel_plan"](root, value, SCHEMA, "run-1")
            for mutate in (
                lambda item: item.update(extra=True),
                lambda item: item.update(schema_version="9"),
                lambda item: item.update(created_at=7),
            ):
                bundle = H["parallel_bundle"](value, freeze); mutate(bundle["freeze"])
                with self.assertRaisesRegex(HarnessError, "freeze"):
                    H["validate_parallel_bundle"](root, bundle, SCHEMA)
            for mutate in (
                lambda state: state.update(guard={"recovered_samples": 2, "admission_open": True}),
                lambda state: state.update(status="READY_TO_INTEGRATE"),
                lambda state: state.update(revision=0),
                lambda state: state.update(revision=999999),
                lambda state: state.update(updated_at=7),
                lambda state: state.update(updated_at="2000-01-01T00:00:00+00:00"),
                lambda state: state.update(guard={"recovered_samples": 0, "admission_open": "yes"}),
                lambda state: state.update(guard={"recovered_samples": 0, "admission_open": False}),
            ):
                bundle = H["parallel_bundle"](value, freeze); mutate(bundle["state"])
                with self.assertRaisesRegex(HarnessError, "state|guard"):
                    H["validate_parallel_bundle"](root, bundle, SCHEMA)

    def test_dependency_readiness_blocks_downstream_worker_admission(self) -> None:
        value = plan(); value["tasks"][2]["depends_on"] = ["left"]
        frozen = {"run_id": "run-1", "base_sha": value["base_sha"],
                  "plan_sha256": H["parallel_plan_digest"](value),
                  "contract_set_id": "parallel-contract-v1:" + "b" * 64}
        state = H["initial_parallel_state"](value, frozen)
        self.assertTrue(H["parallel_task_ready"](state, value, "left"))
        self.assertFalse(H["parallel_task_ready"](state, value, "right"))
        state["tasks"]["left"]["status"] = "validated"
        self.assertTrue(H["parallel_task_ready"](state, value, "right"))

    def test_lock_setup_failure_closes_run_directory_fd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "repo"; root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            control = H["parallel_control_root"](root, "run-1")
            (control / "state.lock").mkdir()
            before = len(os.listdir("/proc/self/fd"))
            for _ in range(20):
                with self.assertRaises(HarnessError):
                    with H["parallel_run_lock"](root, "run-1"):
                        pass
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)
            (control / "state.lock").rmdir()
            (control / "state.lock").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(HarnessError, "lock|size|regular"):
                with H["parallel_run_lock"](root, "run-1"):
                    pass
            (control / "state.lock").unlink()
            real_fstat = os.fstat
            with mock.patch.object(H["os"], "fstat", side_effect=OSError("injected")):
                before = len(os.listdir("/proc/self/fd"))
                for _ in range(10):
                    with self.assertRaises(HarnessError):
                        with H["parallel_run_lock"](root, "run-1"):
                            pass
                self.assertEqual(len(os.listdir("/proc/self/fd")), before)
            import fcntl
            real_flock = fcntl.flock
            def fail_unlock(fd, operation):
                if operation == fcntl.LOCK_UN:
                    raise OSError("injected unlock failure")
                return real_flock(fd, operation)
            before = len(os.listdir("/proc/self/fd"))
            with mock.patch.object(fcntl, "flock", side_effect=fail_unlock):
                for _ in range(10):
                    with self.assertRaises(HarnessError):
                        with H["parallel_run_lock"](root, "run-1"):
                            pass
                with self.assertRaisesRegex(ValueError, "body-primary"):
                    with H["parallel_run_lock"](root, "run-1"):
                        raise ValueError("body-primary")
                with self.assertRaisesRegex(OSError, "body-os-primary"):
                    with H["parallel_run_lock"](root, "run-1"):
                        raise OSError("body-os-primary")
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_canonical_files_reject_special_or_multilink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                (directory / "run.json").mkdir()
                with self.assertRaisesRegex(HarnessError, "regular|canonical"):
                    H["read_parallel_bundle"](dir_fd)
                (directory / "run.json").rmdir()
                (directory / "run.json").write_text("{}", encoding="utf-8")
                os.link(directory / "run.json", directory / "alias.json")
                with self.assertRaisesRegex(HarnessError, "link|canonical"):
                    H["read_parallel_bundle"](dir_fd)
            finally:
                os.close(dir_fd)

    def test_worker_containment_identity_is_live_stable_and_drained_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            token = "a" * 32
            cgroup = Path(raw) / f"verified-agent-harness-{token}"
            cgroup.mkdir()
            for name, data in (("cgroup.freeze", "0"), ("cgroup.events", "populated 1\nfrozen 0\n"),
                               ("cgroup.procs", "123\n"), ("cgroup.kill", "")):
                (cgroup / name).write_text(data, encoding="ascii")
            info = cgroup.stat()
            worker = {"id": "left", "task_id": "left", "run_id": "run-1", "generation": 3,
                      "cgroup": str(cgroup), "cgroup_device": info.st_dev,
                      "cgroup_inode": info.st_ino, "cgroup_parent_device": cgroup.parent.stat().st_dev,
                      "cgroup_parent_inode": cgroup.parent.stat().st_ino,
                      "containment_token": token, "activated": False,
                      "resource_class": "light", "paused": False}
            state = {"run_id": "run-1", "generation": 3, "workers": {"left": worker}}
            tasks = {"left": {"resource_class": "light"}}
            with mock.patch.dict(H, {"validate_worker_cgroup": lambda path: path.resolve()}):
                H["validate_worker_set"](state, tasks)
                with self.assertRaisesRegex(HarnessError, "never activated"):
                    H["retire_parallel_worker"](state, "left")
                H["activate_parallel_worker"](state, "left")
                self.assertTrue(worker["activated"])
                (cgroup / "cgroup.events").write_text("populated 1\nfrozen 1\n", encoding="ascii")
                with self.assertRaisesRegex(HarnessError, "paused|freeze"):
                    H["validate_worker_set"](state, tasks)
                H["validate_worker_set"](state, tasks, reconcile_paused=True)
                self.assertTrue(worker["paused"])
                H["validate_worker_set"](state, tasks)
                worker["paused"] = False
                (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
                with self.assertRaisesRegex(HarnessError, "drain|empty"):
                    H["retire_parallel_worker"](state, "left")
                (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
                H["retire_parallel_worker"](state, "left")
            self.assertNotIn("left", state["workers"])

    def test_memory_guard_blocks_until_pressure_recovers(self) -> None:
        gib = 1024**3
        critical = {"total_bytes": 16*gib, "available_bytes": gib // 2,
                    "swap_free_bytes": 128*1024**2, "psi_some_avg10": 12.0,
                    "psi_full_avg10": 2.0}
        value = {"workers": [{"id": "w", "paused": True, "activated": True,
                              "cgroup": "/fake"}]}
        with mock.patch.dict(H, {"validate_worker_cgroup": lambda path: Path("/fake"),
                                 "worker_cgroup_controls_ready": lambda path: True,
                                 "apply_parallel_memory_action": lambda action, workers: ["w"]}):
            event = H["memory_guard_event"](value, sampler=lambda: critical)
        self.assertFalse(event["safe_to_continue"])
        self.assertFalse(event["admission_open"])

    def test_bound_cgroup_action_uses_retained_directory_fd(self) -> None:
        token = "b" * 32
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw); cgroup = parent / f"verified-agent-harness-{token}"; cgroup.mkdir()
            for name, data in (("cgroup.freeze", "0"), ("cgroup.events", "populated 1\nfrozen 1\n"),
                               ("cgroup.procs", "1\n"), ("cgroup.kill", "")):
                (cgroup / name).write_text(data, encoding="ascii")
            ci, pi = cgroup.stat(), parent.stat()
            worker = {"cgroup": str(cgroup), "cgroup_device": ci.st_dev,
                      "cgroup_inode": ci.st_ino, "cgroup_parent_device": pi.st_dev,
                      "cgroup_parent_inode": pi.st_ino, "containment_token": token,
                      "activated": True}
            with mock.patch.dict(H, {"validate_worker_cgroup": lambda path: path}):
                H["set_bound_worker_frozen"](worker, True)
            self.assertEqual((cgroup / "cgroup.freeze").read_text(encoding="ascii"), "1")

    def test_create_only_publication_never_overwrites_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw); dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                (directory / "run.json").write_text("original", encoding="utf-8")
                with self.assertRaisesRegex(HarnessError, "already frozen"):
                    H["write_parallel_bundle"](dir_fd, {"new": True}, create_only=True)
                self.assertEqual((directory / "run.json").read_text(encoding="utf-8"), "original")
                (directory / "run.json").unlink(); os.mkfifo(directory / "run.json")
                with self.assertRaises(HarnessError):
                    H["write_parallel_bundle"](dir_fd, {"new": True})
            finally:
                os.close(dir_fd)

    def test_publication_failure_reports_whether_canonical_state_was_published(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw); dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                (directory / "run.json").write_text("{}\n", encoding="utf-8")
                real_fsync = os.fsync
                def fail_directory_fsync(fd):
                    if fd == dir_fd:
                        raise OSError("injected directory fsync failure")
                    return real_fsync(fd)
                with mock.patch.object(H["os"], "fsync", side_effect=fail_directory_fsync):
                    with self.assertRaises(HarnessError) as caught:
                        H["write_parallel_bundle"](dir_fd, {"worker": "bound"})
                self.assertTrue(getattr(caught.exception, "canonical_published", False))
                self.assertEqual(json.loads((directory / "run.json").read_text()),
                                 {"worker": "bound"})

                real_close = os.close
                primary_seen = False
                def fail_fsync_then_mark(fd):
                    nonlocal primary_seen
                    if fd == dir_fd:
                        primary_seen = True
                        raise OSError("fsync-primary")
                    return real_fsync(fd)
                def fail_close_after_primary(fd):
                    if primary_seen:
                        raise OSError("close-secondary")
                    return real_close(fd)
                with (mock.patch.object(H["os"], "fsync", side_effect=fail_fsync_then_mark),
                      mock.patch.object(H["os"], "close", side_effect=fail_close_after_primary)):
                    with self.assertRaisesRegex(HarnessError, "publish") as caught:
                        H["write_parallel_bundle"](dir_fd, {"worker": "still-bound"})
                self.assertTrue(getattr(caught.exception, "canonical_published", False))
                self.assertEqual(getattr(caught.exception, "canonical_publication", None),
                                 "published")
                self.assertEqual(json.loads((directory / "run.json").read_text()),
                                 {"worker": "still-bound"})

                real_replace = os.replace
                def replace_then_fail(*args, **kwargs):
                    real_replace(*args, **kwargs)
                    raise OSError("replace-post-effect")
                with mock.patch.object(H["os"], "replace", side_effect=replace_then_fail):
                    with self.assertRaisesRegex(HarnessError, "publish") as caught:
                        H["write_parallel_bundle"](dir_fd, {"worker": "ambiguous-bound"})
                self.assertEqual(getattr(caught.exception, "canonical_publication", None),
                                 "ambiguous")
                self.assertTrue(getattr(caught.exception, "canonical_published", False))
                self.assertEqual(json.loads((directory / "run.json").read_text()),
                                 {"worker": "ambiguous-bound"})
            finally:
                os.close(dir_fd)


if __name__ == "__main__":
    unittest.main()
