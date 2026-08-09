"""Regression coverage for fail-closed semantic handoff rejection."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "skill" / "scripts" / "test_harness.py"
SPEC = importlib.util.spec_from_file_location("handoff_test_support", SUITE)
assert SPEC is not None and SPEC.loader is not None
SUPPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPPORT)
H = SUPPORT.H
HarnessError = SUPPORT.HarnessError


class InvalidHandoffRecoveryTests(unittest.TestCase):
    def test_invalid_verifier_policy_transitions_to_retryable_blocked_state(self) -> None:
        fixture = SUPPORT.StateAndSnapshotTests(methodName="runTest")
        temp, root, paths = fixture.make_harness_repo()
        self.addCleanup(temp.cleanup)
        generation = "d" * 32
        state = H["load_state"](paths)
        base, candidate = H["candidate_identity"](root, H["git_head"](root))
        medium = fixture.finding("R-medium", severity="medium", blocking=True)
        report_path = paths["runtime"] / f"verifier-{generation}.json"
        state.update({
            "workflow_state": "VERIFYING",
            "attempt": 1,
            "base_sha": base,
            "candidate_id": candidate,
            "assessor_findings": {"reviewer": [medium]},
            "owner": {"pid": os.getpid(), "identity": H["process_identity"](os.getpid()),
                      "generation": generation},
            "worker": {"role": "verifier", "pid": os.getpid(), "status": "running",
                       "generation": generation, "stage_id": state["stage_id"],
                       "slice_id": state["slice_id"], "attempt": 1,
                       "base_sha": base, "candidate_id": candidate,
                       "expected_output": str(report_path)},
        })
        H["save_state"](paths, state)
        report = fixture.bound_report(root, state, "verifier")
        report["classifications"] = [{
            "finding_id": "R-medium",
            "classification": "confirmed",
            "policy_blocking": True,
            "rationale": "synthetic invalid policy",
            "evidence": [],
        }]
        report["decision"] = "changes_required"

        with self.assertRaisesRegex(HarnessError, "policy_blocking"):
            with H["state_lock"](paths):
                current = H["load_state"](paths)
                H["validate_worker_handoff_or_block"](
                    root, paths, current, report, "verifier", generation,
                    report_path, os.getpid(),
                )

        final = H["load_state"](paths)
        self.assertEqual(final["workflow_state"], "BLOCKED")
        self.assertEqual(final["worker"]["status"], "failed")
        self.assertEqual(H["load_json"](paths["trusted_checkpoint"])["worker_status"], "failed")


if __name__ == "__main__":
    unittest.main()