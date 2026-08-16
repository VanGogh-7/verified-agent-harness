---
name: codex-harness
description: Run or resume a bounded Codex CLI process with WIP=1, timeout handling, and inspectable local runtime evidence. Use only when a task needs Codex-specific process control; do not use it as a planning, testing, review, approval, Git, or project-lifecycle methodology.
---

# Codex runtime harness

This Skill is intentionally thin. It controls Codex at runtime; it does not define how to engineer software.

## Route first

Select only the installed Addy Skills relevant to the task; do not preload the full catalog. For example:

- bug fix → `debugging-and-error-recovery` and `test-driven-development`
- feature → `planning-and-task-breakdown`, `incremental-implementation`, and `test-driven-development`
- security review → `security-and-hardening`
- final cleanup → `code-review-and-quality` and `code-simplification`
- Git operations → `git-workflow-and-versioning`
- context or uncertainty → `context-engineering` or bounded `doubt-driven-development`

Use `harness-creator` for repository instructions, scope, human-readable task/progress state, executable verification, handoff, and lifecycle. Follow the repository's `AGENTS.md` and verification entrypoint.

Only use this Skill when Codex CLI process control itself is needed.

## Context policy

Treat a Codex thread as ephemeral, task-local working memory. Use one thread for one bounded task and choose deliberately:

- **CONTINUE** — resume the exact thread only for the same objective and implementation or debugging chain. Interrupted work, investigating a test failure, applying its task-local fix, and rerunning verification are continuations.
- **FRESH** — use `run` for a new bounded task, feature, slice, unrelated bug, or materially changed objective. Also prefer fresh context after task completion, when obsolete attempts dominate the thread, or once repository artifacts are the authoritative handoff. Context pressure may justify a fresh start, but no arbitrary token threshold decides it.
- **INDEPENDENT** — always use a fresh `run` when value depends on independent judgment. Never resume the implementation thread for independent code or correctness review, security review, architecture challenge, release approval, or doubt-driven review.

The boundaries `implementation → independent review`, `implementation → security review`, `implementation → architecture challenge`, `implementation → release approval`, and `task A → unrelated task B` require fresh context. `implementation → test failure → debug → fix → rerun` remains CONTINUE.

Persistent project memory belongs in `AGENTS.md`, code, tests, Git history, repository progress/state artifacts, exec plans, handoffs, and verification evidence—not conversation history. If deleting the thread would lose essential project state, improve the repository harness with `harness-creator`; do not turn this runtime wrapper into a state manager.

Single-agent execution is the default: one bounded task, one working thread. When independence is justified, use one bounded fresh-context review that returns findings to the main task or an explicit repair task. Do not create a permanent role pipeline or automatic review loop.

## Runtime contract

`scripts/codex_runtime.py` provides:

- one active run per workspace (WIP=1);
- explicit `read-only` or `workspace-write` sandbox selection;
- hard wall-clock timeout and process-group termination;
- JSONL events, stderr, final response, thread ID, token usage, and result metadata;
- exact-session resume by saved thread ID;
- recovery of abandoned `running` records as `interrupted`.

It has five process states: `running`, `succeeded`, `failed`, `timed_out`, and `interrupted`. These are runtime facts, not task approval states.

Run evidence is stored under `<workspace>/.codex-harness/runs/<run-id>/`. Add `.codex-harness/` to the target repository's ignore rules when evidence should remain local.

## Commands

Pass prompts by file so shell quoting cannot alter them:

```bash
python3 scripts/codex_runtime.py doctor
python3 scripts/codex_runtime.py run \
  --workspace /absolute/repo \
  --prompt-file /absolute/prompt.md \
  --sandbox workspace-write \
  --timeout 1800
python3 scripts/codex_runtime.py status --workspace /absolute/repo
python3 scripts/codex_runtime.py resume \
  --workspace /absolute/repo \
  --run-id <run-id> \
  --prompt-file /absolute/follow-up.md
```

Use `--schema` only when the caller actually needs structured final output. Use `--ephemeral` only when resume is intentionally unavailable. Model and reasoning effort are optional pass-through deployment choices.

## Boundaries

- Do not create mandatory Implementer/Reviewer/Verifier roles.
- Do not treat model prose as verification evidence.
- Do not implement task state, approval, candidate fingerprints, gate policy, lifecycle, planning, TDD, review loops, Git policy, security methodology, or performance methodology here.
- Run repository verification commands directly and record their executable results in the repository-owned progress artifact.
- One independent review may be useful for a high-risk decision; it must be bounded and is not a default pipeline.

Read `references/runtime-contract.md` only when changing the runner or its evidence format.

## Self-verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v scripts.test_runtime
python3 /home/van-gogh/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```
