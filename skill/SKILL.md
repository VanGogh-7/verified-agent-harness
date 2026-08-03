---
name: codex-harness
description: Use when orchestrating staged Hermes + Codex engineering.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [codex, harness, orchestration, review, quality-gates]
    related_skills: [codex, hermes-agent]
---

# Codex Harness Engineering

## Role

Act as Lead Engineer and Orchestrator. Codex A implements with `workspace-write`; independent Codex B reviews with `read-only`; configured formatter/lint/check/test commands are deterministic gates. Git owns history. `.harness/` exposes current project state and replaceable runtime handoffs; the active control-plane copy lives under Git metadata (`.git/harness-control/`), outside Codex's writable sandbox.

## Mandatory protocol

1. Discuss direction, architecture, Stages, Slices, acceptance criteria, and hard exclusions with the user. Do not launch Codex before explicit approval.
2. Run `harness init` once. Before every action, read only `.harness/state.json` (or `harness status`) and the relevant current-state document.
3. Record approval with `harness start-stage --stage ... --title ... --slice ... --plan-file ...`. The command validates transitions and updates `CURRENT_STAGE.md`.
4. Invoke Codex only through `harness run-implementer` or `harness run-reviewer`. Never call `codex exec` directly.
5. Launch long worker commands with Hermes `terminal(background=true, notify_on_complete=true)`. The launch must return a background session identifier quickly. Do not run long Codex work in a foreground tool call and do not call `process(log)`.
6. While a worker runs, trust `harness status` (backed by the canonical Git-metadata checkpoint) and the completion notification only as wake-up signals. Business results come from validated canonical evidence; never read full worker logs by default.
7. After Implementer handoff, run `harness run-gates`. Launch Reviewer only after gates pass. Approve a Slice only when both `quality-gates.json` and `review.json` pass.
8. Limit each Slice to three A/B correction attempts (or the lower configured cap). On exhaustion, stop at `HUMAN_CHECKPOINT`.
9. At Stage completion, update `PROJECT_STATE.md`, run `harness approve-slice --complete-stage`, and let the CLI clear replaceable runtime files. Commit only when the project/user policy authorizes it.

Secure worker execution requires a writable delegated Linux cgroup v2 with
`cgroup.kill`; child-subreaper support is defense in depth. Harness records the
cgroup before launch and empties it before accepting a handoff, including after
an orchestrator crash. A private user/mount/cgroup/PID namespace hides cgroup
controls and the user-manager socket from the worker, then drops all capabilities
and enables no-new-privileges before Codex starts, preventing same-UID migration
out of containment. Harness copies validated implementation/gate/review evidence
into `.git/harness-control/` and never approves from mutable runtime reports.

## Context maintenance during worker idle time

After the background launch returns, ensure Stage/Slice/attempt/next action are durable. Then call `harness_context_status` if available. Compression is allowed only when all safety conditions in `references/recovery.md` hold.

Hermes 0.19.1 has canonical automatic and `/compress` paths but no public plugin API that safely compacts the active conversation after an agent tool call returns. The companion plugin therefore reports occupancy and returns an explicit `manual_required`/automatic-fallback result; it never claims compaction succeeded. If threshold is reached, preserve the checkpoint, finish the current tool turn, use the canonical `/compress` command when the surface permits, then re-read state, checkpoint, and `CURRENT_STAGE.md`. Never compress while a Hermes tool call is outstanding.

## Read-on-demand map

- Full A/B workflow and command recipes: `references/workflow.md`
- Legal states and transitions: `references/state-machine.md`
- Recovery, race handling, checkpoint, and compression safety: `references/recovery.md`
- Handoff contracts: `schemas/*.schema.json`
- Prompt/project starting points: `templates/`

## Completion check

Do not report completion until the structured handoffs validate, deterministic gates pass, independent review passes, the final Git diff is scoped, and all skipped checks and residual risks are stated.
