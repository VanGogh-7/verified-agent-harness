# Executable state machine

## States

- `STAGE_DISCUSSION`: no approved Stage.
- `STAGE_APPROVED`: approval recorded transiently.
- `SLICE_READY`: Implementer may launch.
- `IMPLEMENTING`: the sole writer is active or its handoff is pending.
- `VALIDATING`: Implementer completed; deterministic gates are pending or passed.
- `ASSESSING`: one or more required read-only assessors are pending or complete.
- `VERIFYING`: all required assessor evidence exists and Verifier is active.
- `CHANGES_REQUIRED`: Verifier confirmed a policy-blocking finding, or a deterministic product gate failed.
- `APPROVED`: Verifier approved the complete candidate evidence join; the Orchestrator must select the next Slice or Stage completion.
- `BLOCKED`: infrastructure, missing evidence, or policy-blocking inconclusive evidence needs recovery/human action.
- `HUMAN_CHECKPOINT`: repair budget exhausted.
- `STAGE_COMPLETED`: terminal Stage evidence recorded.

## Legal transitions

```text
STAGE_DISCUSSION -> STAGE_APPROVED
STAGE_APPROVED -> SLICE_READY | BLOCKED
SLICE_READY -> IMPLEMENTING | BLOCKED | HUMAN_CHECKPOINT
IMPLEMENTING -> VALIDATING | BLOCKED | HUMAN_CHECKPOINT
VALIDATING -> ASSESSING | CHANGES_REQUIRED | BLOCKED | HUMAN_CHECKPOINT
ASSESSING -> VERIFYING | BLOCKED | HUMAN_CHECKPOINT
VERIFYING -> CHANGES_REQUIRED | APPROVED | BLOCKED | HUMAN_CHECKPOINT
CHANGES_REQUIRED -> IMPLEMENTING | HUMAN_CHECKPOINT | BLOCKED
APPROVED -> SLICE_READY | STAGE_COMPLETED | CHANGES_REQUIRED
BLOCKED -> SLICE_READY | ASSESSING | CHANGES_REQUIRED | HUMAN_CHECKPOINT
HUMAN_CHECKPOINT -> SLICE_READY | CHANGES_REQUIRED | BLOCKED
STAGE_COMPLETED -> STAGE_APPROVED
```

Assessor completion does not transition to repair or approval. Correctness Reviewer starts `VALIDATING -> ASSESSING`; later assessor reports accumulate in `ASSESSING`. Verifier alone performs `ASSESSING -> VERIFYING`, then makes one decision edge. A new Implementer attempt clears gates, assessor reports, Verifier report, candidate identity, and counters before entering `IMPLEMENTING`.

The narrow `APPROVED -> CHANGES_REQUIRED` edge is used only by operator-authorized `recover --reopen-review` before Slice acceptance.

A required Tester outcome of `flaky_or_infra` enters `BLOCKED`. An authorized infrastructure retry returns directly to `ASSESSING` with the same candidate attempt; it does not create an Implementer repair attempt.
