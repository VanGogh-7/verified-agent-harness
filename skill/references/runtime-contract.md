# Runtime contract

The runner is a transport/process boundary, not an engineering workflow.

Each run directory contains:

| File | Meaning |
|---|---|
| `metadata.json` | Atomic process facts: arguments, timestamps, state, exit code, thread ID, and usage |
| `events.jsonl` | Unmodified JSONL stdout from `codex exec --json` |
| `stderr.log` | Unmodified CLI stderr |
| `final.md` | Final model message when Codex produces one |

WIP is enforced with an advisory `flock` held for the entire child lifetime. The child starts in a new process session so timeout handling terminates the process group, not merely the launcher.

When a new run obtains the lock, any prior record still marked `running` is changed to `interrupted`. This recovery reports what is knowable; it never infers task success or approval.

`run` starts fresh without a thread ID. `resume` uses the exact saved Codex thread ID and records the source run as `parent_run_id`; it never selects a merely related or latest thread. Ephemeral runs cannot be resumed.

Resume only to continue the same bounded objective and implementation or debugging chain. Start fresh for a new task, a materially changed objective, or work that requires independent judgment. In particular, independent code or correctness review, security review, architecture challenge, release approval, and doubt-driven review must not resume the implementation thread. A test failure and its investigation, fix, and rerun remain part of the same task and normally resume.

The runner cannot infer task meaning from process state: `succeeded` means the process exited successfully, not that the repository task is complete. Context choice therefore remains an explicit caller policy rather than a heuristic or hard gate.

Conversation history is temporary working memory, not durable project state. Persist essential context in repository-owned instructions, code, tests, Git history, progress/state artifacts, exec plans, handoffs, and verification evidence. The runner records process evidence only; it does not implement those repository systems.

The wrapper supports only `read-only` and `workspace-write`. It deliberately does not expose `danger-full-access`; callers needing broader authority should invoke Codex explicitly in a separately controlled environment.

The evidence proves what process ran and what it emitted. Correctness must come from the target repository's executable verification commands.
