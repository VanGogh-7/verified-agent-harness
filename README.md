# Thin Codex Harness

This repository contains one small Codex-specific runtime Skill. It is not a software engineering framework.

## Architecture

| Layer | Responsibility | Does not own |
|---|---|---|
| Repository `AGENTS.md` | Persistent, repository-specific rules and boundaries | General engineering methodology |
| Addy agent-skills | Task-relevant planning, implementation, debugging, testing, review, and security methods | Repository state or Codex process control |
| `harness-creator` | Generic repository instructions, scope, state, verification, handoff, and lifecycle artifacts | Codex CLI execution |
| `codex-harness` | One bounded Codex CLI process: start/resume, WIP=1, sandbox forwarding, timeout, termination, and runtime evidence | Engineering workflow or durable project memory |

These are ownership boundaries, not a chain of agents. `codex-harness` does not orchestrate Addy Skills or `harness-creator`; the caller selects the task-relevant method and repository artifacts, then uses this wrapper only when Codex CLI process control is needed.

```text
repository state + bounded task prompt
                  |
                  v
        codex-harness run | resume
                  |
                  v
       one Codex process / working thread
                  |
                  v
      .codex-harness/runs/<run-id>/ evidence
```

The main task normally uses one thread. A justified independent review uses a separate fresh thread, returns bounded findings, and does not create a permanent multi-agent pipeline.

## What remains

`codex-harness` enforces WIP=1, launches `codex exec` with an explicit sandbox, applies a wall-clock timeout to the whole process group, captures runtime evidence, and resumes an exact saved session. Its only states are process facts:

```text
running → succeeded | failed | timed_out | interrupted
```

It does not plan work, prescribe TDD, classify workflow risk, create Implementer/Reviewer/Verifier roles, fingerprint candidates, approve changes, run generic quality gates, manage Git, or own repository lifecycle.

## Context policy

- **CONTINUE:** `resume` an exact thread only for the same bounded objective and implementation/debugging chain.
- **FRESH:** use `run` for a new task, unrelated work, or a materially changed objective.
- **INDEPENDENT:** always use a fresh `run` for independent code/correctness or security review, architecture challenge, release approval, and doubt-driven review.

The repository is durable memory; a Codex thread is temporary task-local working memory. Keep essential state in instructions, code, tests, Git and repository-owned progress, plans, handoffs, and verification evidence. Use only task-relevant Addy Skills. Single-agent execution is the default; a bounded fresh-context review is selective, not a permanent orchestration pipeline.

## Method and repository harness

Use the installed Addy Skills directly for planning, incremental implementation, TDD, debugging, review, security, performance, Git discipline, Definition of Done, context engineering, and bounded doubt-driven review.

Use `harness-creator` for `AGENTS.md`, human-readable feature/progress state, scope, executable verification entrypoints, handoff, bootstrap, adoption, repair, and lifecycle. Verification commands should run directly and their results should be recorded in the repository-owned progress artifact.

## Install

The installer creates safe symlinks for the Skill and launcher. It preserves an existing identical link and refuses to replace unrelated targets.

```bash
scripts/install-codex --dry-run
scripts/install-codex
```

The small `lifecycle-skill/SKILL.md` compatibility router remains so older installed links and repository instructions direct lifecycle work to `harness-creator`. It contains no lifecycle implementation.

## Repository layout

```text
skill/SKILL.md                         routing, context policy, and runtime boundaries
skill/scripts/codex_runtime.py         thin Codex CLI process wrapper
skill/references/runtime-contract.md   evidence and exact-resume contract
skill/scripts/test_runtime.py          runtime regression tests
bin/codex-harness                      installed command launcher
scripts/install-codex                  non-destructive symlink installer
lifecycle-skill/SKILL.md               compatibility router to harness-creator
tests/test_integration.py              launcher and installer integration tests
```

## Use

```bash
codex-harness doctor
codex-harness run \
  --workspace /absolute/repository \
  --prompt-file /absolute/task.md \
  --sandbox workspace-write \
  --timeout 1800
codex-harness status --workspace /absolute/repository
codex-harness resume \
  --workspace /absolute/repository \
  --run-id <run-id> \
  --prompt-file /absolute/follow-up.md
```

Per-run evidence is written to `.codex-harness/runs/<run-id>/`: atomic metadata, raw JSONL events, stderr, and the final response. This evidence describes the process; repository tests and checks determine correctness.

## Verification

```bash
./init.sh
python3 /home/van-gogh/.codex/skills/.system/skill-creator/scripts/quick_validate.py skill
python3 /home/van-gogh/.codex/skills/.system/skill-creator/scripts/quick_validate.py lifecycle-skill
git diff --check
```

The runtime contract follows the official Codex CLI non-interactive interface: explicit sandbox, JSONL events, last-message capture, structured output when requested, and exact-session resume. See the [official non-interactive documentation](https://developers.openai.com/codex/noninteractive) and [CLI reference](https://developers.openai.com/codex/cli/reference).

Licensed under the [MIT License](LICENSE).
