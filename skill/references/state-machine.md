# State machine

## States

- `STAGE_DISCUSSION`: no approved work may launch.
- `STAGE_APPROVED`: user approval has been durably recorded.
- `SLICE_READY`: an approved Slice may launch Implementer A.
- `IMPLEMENTING`: A is running or its handoff is pending.
- `VALIDATING`: A completed; deterministic gates are next/current.
- `REVIEWING`: gates passed; Reviewer B is running or has approved.
- `CHANGES_REQUIRED`: a gate or Reviewer requires another A attempt.
- `APPROVED`: current Slice has passing gate and review evidence.
- `BLOCKED`: worker/tooling/handoff failure needs recovery.
- `HUMAN_CHECKPOINT`: attempt cap or irreconcilable condition requires a human decision.
- `STAGE_COMPLETED`: all Stage criteria passed and durable project state was updated.

## Legal transitions

```text
STAGE_DISCUSSION -> STAGE_APPROVED
STAGE_APPROVED -> SLICE_READY | BLOCKED
SLICE_READY -> IMPLEMENTING | BLOCKED | HUMAN_CHECKPOINT
IMPLEMENTING -> VALIDATING | BLOCKED | HUMAN_CHECKPOINT
VALIDATING -> REVIEWING | CHANGES_REQUIRED | BLOCKED | HUMAN_CHECKPOINT
REVIEWING -> CHANGES_REQUIRED | APPROVED | BLOCKED | HUMAN_CHECKPOINT
CHANGES_REQUIRED -> IMPLEMENTING | HUMAN_CHECKPOINT | BLOCKED
APPROVED -> SLICE_READY | STAGE_COMPLETED
BLOCKED -> SLICE_READY | CHANGES_REQUIRED | HUMAN_CHECKPOINT
HUMAN_CHECKPOINT -> SLICE_READY | CHANGES_REQUIRED | BLOCKED
STAGE_COMPLETED -> STAGE_APPROVED
```

`APPROVED` is recorded as an audit event inside the final approval transaction,
not exposed as a separately durable stopping point. For a next Slice, Harness
writes the idempotent Stage document first and then atomically persists the
`REVIEWING -> APPROVED -> SLICE_READY` state update. A crash before that state
write leaves `REVIEWING`, so the same approval command can be retried. Stage
completion similarly writes an idempotent completion marker before atomically
persisting `STAGE_COMPLETED`; runtime cleanup occurs afterward and can be
repeated with `reset-runtime`.

The CLI validates every edge. `start-stage` records the two edges `STAGE_DISCUSSION/STAGE_COMPLETED -> STAGE_APPROVED -> SLICE_READY` in order. Passing gates record a credential-safe fingerprint of changed worktree content. `approve-slice` revalidates the implementation, gate, and review reports, that worktree fingerprint, and the protected-path/Git-HEAD baseline before recording the approval and its explicitly selected next-Slice or Stage-complete target in one final state write. `recover --retry` and `recover --ack-human` are the human-authorized exits from `BLOCKED` and `HUMAN_CHECKPOINT`.

State writes use a same-directory temporary file, file `fsync`, `os.replace`, and directory `fsync` on POSIX. The canonical active state, lock, and validated approval evidence live in `.git/harness-control/`; `.harness/state.json` and runtime reports are repairable/diagnostic project-facing mirrors. Codex sandboxes cannot write Git metadata. The CLI excludes its own control directory from the Git-metadata baseline while protecting all other Git metadata. Runtime files are current-run replaceable artifacts; Git is the history mechanism.
