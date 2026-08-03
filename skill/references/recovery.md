# Recovery and idle-time context maintenance

## Worker recovery

Run `harness recover` after interruption. It reads current state and `orchestrator-checkpoint.json`. Every run has a random generation, and the checkpoint binds Stage, Slice, attempt, role, generation, worker identity, and Harness-owner PID identity. Recovery refuses takeover while that exact owner is alive. Only after confirmed owner death does it reclaim an optional cgroup/process tree and consider the generation-specific handoff. Stale Reviewer, attempt, or generation evidence is rejected.

- live PID: report `running`; never launch a duplicate;
- stopped PID plus valid expected handoff: reconcile the legal next state;
- stopped PID without valid handoff: transition active work to `BLOCKED`;
- handoff found while already at `BLOCKED`/`HUMAN_CHECKPOINT`: report it for human review without jumping a terminal checkpoint.

After a human decision, use `harness recover --retry` to resume `BLOCKED`, or `harness recover --ack-human` to acknowledge `HUMAN_CHECKPOINT` and reset the attempt counter. These flags are the only supported terminal-checkpoint exits; never edit `state.json` manually.

`harness reset-runtime` is accepted only in `STAGE_DISCUSSION` or `STAGE_COMPLETED`. It deletes only children of the exact `.harness/runtime/` directory and preserves project config, state, and Markdown files. Stage completion uses the same bounded cleanup after approval has revalidated all three canonical evidence reports and protected-path/Git-HEAD invariants. Completion documentation is idempotent and is written before the final state commit; cleanup is repeatable after that commit, so no crash leaves a durable intermediate `APPROVED` state.

## Orchestrator checkpoint

Before/after worker launch the Harness atomically overwrites one canonical checkpoint containing Stage, Slice, state, role, generation, owner and worker PID identities, optional cgroup identity, attempt, generation-specific handoff, next action, unresolved findings, user constraints, compaction count, and timestamp. State carries a monotonic revision and updates use compare-and-swap checks. There are no per-attempt checkpoint archives.

All Harness-owned runtime writes first open `.harness/` and then `runtime/` with `O_DIRECTORY | O_NOFOLLOW`, and operate relative to the resulting descriptor. Replacing the runtime directory with a symlink therefore fails closed rather than turning the host Harness into a confused-deputy writer. `recover` may inspect the canonical checkpoint without trusting that runtime path and moves the active workflow to `BLOCKED`; every other command continues to reject the symlink.

After launch, persistence, stream, and handoff failures terminate and reap the
complete worker tree before recording `BLOCKED`. `recover --retry` checks the
recorded PID identity before allowing another attempt even if the runtime
checkpoint is unavailable. Runtime cleanup recursively unlinks through
no-follow directory descriptors and never follows child symlinks.

## Compression safety boundary

All conditions must hold:

1. Worker entered background execution and the Hermes launch tool call returned.
2. No Hermes tool call is outstanding.
3. Stage/Slice/attempt/next action are durable.
4. Complete worker output is redirected to runtime logs.
5. The final result will be written to the expected structured handoff.
6. The checkpoint says this worker has not exceeded `max_compactions_per_worker_run`.

Never compact inside the `harness_compact_if_needed` tool call: that tool call itself is outstanding. Never invoke private host compression methods from a plugin worker thread. Preserve completion notifications only as wakeups; the handoff file is authoritative.

## Current Hermes 0.19.1 compatibility

Verified canonical mechanisms:

- automatic preflight and idle-on-resume compaction use `AIAgent._compress_context` and the configured Context Engine;
- manual `/compress` uses the same guarded commit/session-rotation path;
- Context Engine plugins can replace compression behavior but do not provide an agent-callable active-session compaction service;
- PluginContext does not expose a stable current-agent/context-compression API.

The companion plugin uses a version-pinned, read-only lookup of the CLI agent only to estimate occupancy with Hermes' canonical token estimator. It does not mutate transcripts. `harness_compact_if_needed` returns one of: `below_threshold`, `automatic_fallback`, `manual_required`, or `unsupported`; only a future verified public host API may return `compacted`.

If compatibility fails, `harness doctor` reports degraded and the plugin fails closed. Rely on built-in automatic compression or finish the current tool turn and invoke `/compress` manually. After compaction, re-read `.harness/state.json`, the orchestrator checkpoint, and `CURRENT_STAGE.md`; verify PID/handoff before doing anything else.

## Completion-notification race

Codex may finish during compression. Do not restart it. After context recovery:

1. reload durable state and checkpoint;
2. check the recorded PID or expected handoff;
3. treat the handoff as authoritative even if notification ordering differed;
4. continue with gates/review/approval from the reconciled state.
