from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from skill.scripts import test_harness as regression


H = regression.H
HarnessError = regression.HarnessError


class RecoveryHardeningTests(unittest.TestCase):
    def make_repo(self):
        return regression.StateAndSnapshotTests.make_repo(self)

    def make_harness_repo(self):
        return regression.StateAndSnapshotTests.make_harness_repo(self)

    def test_trusted_preflight_is_bounded_and_fail_closed(self) -> None:
        cfg = {"agent_runtime": {
            "adapter_argv": ["/opt/harness/adapters/reference"],
            "ephemeral": True,
            "preflight_argv": [sys.executable, "-c", "raise SystemExit(0)"],
            "preflight_timeout_seconds": 7,
        }}
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(H["subprocess"], "run") as run:
            root = Path(raw)
            run.return_value = types.SimpleNamespace(returncode=0)
            result = H["run_agent_preflight"](cfg, root)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(run.call_args.args[0], cfg["agent_runtime"]["preflight_argv"])
            self.assertEqual(run.call_args.kwargs["timeout"], 7)
            self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            run.return_value = types.SimpleNamespace(returncode=9)
            with self.assertRaisesRegex(HarnessError, "trusted agent preflight failed"):
                H["run_agent_preflight"](cfg, root)
            run.side_effect = subprocess.TimeoutExpired(cfg["agent_runtime"]["preflight_argv"], 7)
            with self.assertRaisesRegex(HarnessError, "trusted agent preflight timed out"):
                H["run_agent_preflight"](cfg, root)
        cfg["agent_runtime"]["preflight_argv"] = "not-an-array"
        with self.assertRaisesRegex(HarnessError, "preflight_argv"):
            H["run_agent_preflight"](cfg, Path("/work/project"))

    def test_preflight_failure_precedes_attempt_and_worker_identity(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        with contextlib.chdir(root), mock.patch.dict(
            H, {"run_agent_preflight": mock.Mock(side_effect=HarnessError("preflight denied"))}
        ), self.assertRaisesRegex(HarnessError, "preflight denied"):
            H["run_worker"](argparse.Namespace(dry_run=False), "implementer")
        state = H["load_state"](paths)
        self.assertEqual((state["workflow_state"], state["attempt"], state["worker_run_seq"]),
                         ("SLICE_READY", 0, 0))
        self.assertIsNone(state["owner"])
        self.assertIsNone(state["worker"])

    def test_exhausted_attempt_enters_human_checkpoint_before_preflight(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"workflow_state": "CHANGES_REQUIRED", "attempt": 3,
                      "owner": None, "worker": None})
        H["save_state"](paths, state)
        preflight = mock.Mock(side_effect=HarnessError("preflight must not run"))
        with contextlib.chdir(root), mock.patch.dict(H, {"run_agent_preflight": preflight}), \
                self.assertRaisesRegex(HarnessError, "max_slice_attempts exceeded"):
            H["run_worker"](argparse.Namespace(dry_run=False), "implementer")
        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "HUMAN_CHECKPOINT")
        self.assertEqual(final["attempt"], 3)
        self.assertEqual(final["worker_run_seq"], 0)
        self.assertIsNone(final["owner"])
        self.assertIsNone(final["worker"])
        preflight.assert_not_called()

    def test_ack_human_drains_recorded_containment_before_clearing_identity(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        cgroup = Path("/sys/fs/cgroup/verified-agent-harness-synthetic")
        state = H["load_state"](paths)
        state.update({"workflow_state": "HUMAN_CHECKPOINT", "attempt": 3, "owner": None,
                      "worker": {"role": "implementer", "pid": 999_999_999,
                                 "process_identity": "dead", "status": "failed",
                                 "cgroup": str(cgroup)}})
        H["save_state"](paths, state)
        destroy = mock.Mock()
        with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(H, {"destroy_worker_cgroup": destroy}):
            H["recover"](argparse.Namespace(retry=False, ack_human=True, reopen_review=False,
                                             reason=None, plan_file=None))
        destroy.assert_called_once_with(cgroup, kill=True)
        self.assertIsNone(H["load_state"](paths)["worker"])

    def test_ack_human_refuses_live_uncontained_worker(self) -> None:
        temp, root, paths = self.make_harness_repo()
        self.addCleanup(temp.cleanup)
        state = H["load_state"](paths)
        state.update({"workflow_state": "HUMAN_CHECKPOINT", "attempt": 3, "owner": None,
                      "worker": {"role": "implementer", "pid": 4242,
                                 "process_identity": "live", "status": "failed", "cgroup": None}})
        H["save_state"](paths, state)
        with contextlib.chdir(root), mock.patch.dict(
            H, {"process_alive": lambda pid, identity: pid == 4242 and identity == "live"}
        ), self.assertRaisesRegex(HarnessError, "recorded worker process is still alive"):
            H["recover"](argparse.Namespace(retry=False, ack_human=True, reopen_review=False,
                                             reason=None, plan_file=None))
        self.assertEqual(H["load_state"](paths)["workflow_state"], "HUMAN_CHECKPOINT")

    def test_ack_human_refuses_live_worker_after_stale_cgroup_drain(self) -> None:
        worker = {"pid": 4242, "process_identity": {"start_time": "1"},
                  "cgroup": "/sys/fs/cgroup/stale-harness-worker"}
        destroy = mock.Mock()
        with mock.patch.dict(H, {
            "destroy_worker_cgroup": destroy,
            "process_alive": lambda _pid, _identity: True,
        }), self.assertRaisesRegex(HarnessError, "recorded worker process is still alive"):
            H["drain_recorded_worker"](worker, "acknowledge")
        destroy.assert_called_once_with(Path(worker["cgroup"]), kill=True)

    def test_failed_assessors_retry_same_candidate_preserve_peers_and_relaunch(self) -> None:
        cases = (("reviewer", [], "ASSESSING"),
                 ("tester", ["reviewer"], "ASSESSING"),
                 ("security_reviewer", ["reviewer", "tester"], "ASSESSING"))
        for role, completed, target in cases:
            with self.subTest(role=role):
                temp, root, paths = self.make_harness_repo()
                self.addCleanup(temp.cleanup)
                state = H["load_state"](paths)
                base = H["git_head"](root)
                _, candidate = H["candidate_identity"](root, base)
                state.update({"workflow_state": "BLOCKED", "attempt": 2, "base_sha": base,
                              "candidate_id": candidate, "gates_passed": True,
                              "gate_level": "stage", "completed_assessments": list(completed),
                              "required_assessments": (["reviewer", "tester", "security_reviewer"]
                                                       if role == "security_reviewer"
                                                       else ["reviewer", "tester"]),
                              "workflow": ("SECURITY" if role == "security_reviewer" else "VERIFIED"),
                              "owner": None, "worker": {"role": role, "status": "failed",
                                                          "pid": None, "cgroup": None}})
                H["save_state"](paths, state)
                with contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
                    H["recover"](argparse.Namespace(retry=True, ack_human=False,
                                                     reopen_review=False, reason=None, plan_file=None))
                final = H["load_state"](paths)
                self.assertEqual((final["workflow_state"], final["attempt"], final["candidate_id"]),
                                 (target, 2, candidate))
                self.assertEqual(final["completed_assessments"], completed)

                # Prove run_worker admits the recovered role and reaches the bounded
                # launch preflight, rather than leaving a state-machine dead end.
                admitted = mock.Mock(side_effect=HarnessError(f"{role} relaunch admitted"))
                with contextlib.chdir(root), mock.patch.dict(H, {"run_agent_preflight": admitted}), \
                        self.assertRaisesRegex(HarnessError, f"{role} relaunch admitted"):
                    H["run_worker"](argparse.Namespace(dry_run=False), role)
                admitted.assert_called_once()

    def test_candidate_identity_requires_consecutive_stable_fingerprints(self) -> None:
        with mock.patch.dict(H, {"worktree_fingerprint": mock.Mock(
            side_effect=("first", "second", "second")
        )}):
            base, candidate = H["candidate_identity"](Path("/work/project"), "a" * 40)
        expected = H["hashlib"].sha256(f"candidate-v1\0{base}\0second".encode()).hexdigest()
        self.assertEqual(candidate, f"candidate-v1:{expected}")
        with mock.patch.dict(H, {"worktree_fingerprint": mock.Mock(
            side_effect=("one", "two", "three", "four")
        )}), self.assertRaisesRegex(HarnessError, "worktree changing during candidate capture"):
            H["candidate_identity"](Path("/work/project"), "b" * 40)


if __name__ == "__main__":
    unittest.main()
