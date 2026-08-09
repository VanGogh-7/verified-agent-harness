---
name: verified-agent-harness
description: Classify and execute trusted-local, evidence-bound engineering workflows through a provider-neutral agent adapter.
version: 1.0.0
author: verified-agent-harness contributors
license: MIT
platforms: [linux]
---

# verified-agent-harness 1.0

## Purpose

Act as the Orchestrator for a trusted-local engineering workflow. The inner Harness enforces:

```text
one writer -> deterministic gates -> independent read-only assessors -> independent verifier -> approval
```

Git HEAD plus a credential-safe worktree fingerprint forms `candidate_id`. A new candidate invalidates every old gate, assessor, and Verifier report. Slice candidates may remain uncommitted; commits remain a Stage-level policy.

## Classify first

- `FAST`: trivial, localized, low-risk work. Use one writer and direct deterministic checks; do not initialize the Stage engine solely for FAST work.
- `VERIFIED`: default for nontrivial work. Use Implementer, gates, Correctness Reviewer, Tester, Verifier.
- `SECURITY`: VERIFIED plus Security Reviewer. Select it with `start-stage --workflow SECURITY`; it cannot be toggled during an active Stage.
- `DAG`: genuinely independent writers use isolated worktrees and optional outer coordination. Each candidate still passes this inner verified gate before integration.
- `LONG_RUNNING`: use durable Kanban tasks, event-driven waiting, and explicit completion contracts around the same inner gate.

Kanban is an optional outer durable DAG. Its flexible metadata is never a trusted approval boundary and does not replace `.git/harness-control/`.

## Authority

- Implementer is the only writer. It may edit only the approved worktree scope.
- Gates execute configured deterministic commands without a shell.
- Correctness Reviewer, Tester, optional Security Reviewer, and Verifier are policy read-only. The configured trusted adapter must enforce that access mode; the core independently rejects their evidence if the candidate changed, but does not provide kernel-level containment for an arbitrary adapter.
- Assessor findings are hypotheses. Assessors cannot approve or force repair.
- Verifier classifies each finding as `confirmed`, `rejected`, `inconclusive`, or `flaky_or_infra`.
- Only confirmed policy-blocking findings cause `CHANGES_REQUIRED`. Policy-blocking inconclusive or `flaky_or_infra` evidence fails closed to `BLOCKED`; rejected findings do not cause repair.
- The Orchestrator alone starts Stages, runs gates, accepts approval, changes policy between Stages, and owns external coordination.

All roles deny secrets and network authority by default. No role may merge, commit, push, publish, tag, release, or alter protected control files unless a separately approved project policy grants the specific action. No outbound telemetry exists; concise local evidence counters are allowed.

## Protocol

1. Obtain explicit scope and acceptance approval. When lifecycle routing is required, use the repository-installed `bin/harness` router; this Stage Skill intentionally exposes no `detect`, `assess`, `bootstrap`, `adopt`, or `activate` command.
2. `harness init`, then `harness start-stage --workflow VERIFIED|SECURITY ...`.
3. Run Implementer. It records schema version, attempt, `base_sha`, final `candidate_id`, changed files, command results, decisions, limitations, residual risk, and blockers.
4. Run deterministic gates. Slice or Stage gates bind their evidence to the same candidate.
5. Run required assessors. Logical fan-out may execute serially whenever host resources do not safely allow concurrency.
6. Run Verifier only after every required assessor report exists.
7. Run `approve-slice` only after Verifier approval. Approval revalidates all canonical reports, candidate identity, protected baseline, Git HEAD, and current worktree.

Use the configured agent runtime adapter for real workers, bind any external session to the active generation, and treat notifications only as wakeups. Read canonical state again after every wakeup. Dry runs produce only runtime artifacts and never mutate business state or satisfy approval.

## Candidate and evidence policy

`harness candidate-id --json` returns the current identity. The identity is derived from `candidate-v1`, the Stage baseline `base_sha`, and exact changed worktree content. Credential-shaped files contribute metadata only; their contents are never read. All role reports repeat attempt, base, and candidate. Mismatch fails closed for assessors, Verifier, and approval.

Canonical state and validated evidence live under `.git/harness-control/`. Project-facing `.harness/state.json` and runtime reports are mirrors. The protected baseline binds Git HEAD, stable Git configuration, and configured protected files. State uses atomic replace, fsync, a lock, and revision checks.

Retain source-system JSON or raw logs for external claims, plus revision identity and integrity evidence. Plan prose is not evidence. No hosted CI, app-server, A2A, MCP, publication provider, or distributed scheduler is implemented by this Skill.

## Repair and resource policy

The default budget is the initial candidate plus two repair rounds (`max_slice_attempts = 3`). Only a positively classified adapter spawn/exec or structured-output startup rejection before business work, with an unchanged worktree, releases a tentative Implementer attempt. Classification fails closed: timeouts, ordinary nonzero exits, worktree changes, invalid or missing handoffs, and runtime-integrity failures remain charged. A confirmed blocker consumes the next Implementer round; exhaustion enters `HUMAN_CHECKPOINT`.

Never overlap a live Worker or Reviewer with another heavy process; the same rule covers Tester, Security Reviewer, Verifier, builds, tests, benchmarks, and packaging. Check RAM, swap trend, GPU headroom when relevant, and live agent descendants before launch. `CARGO_BUILD_JOBS=4` is the default cap. Logical assessor fan-out may be serial.

Worker generation, Stage, Slice, attempt, candidate, owner/worker identities, session, and checkpoint epoch are recovery bindings. Stale evidence is rejected for every role. See `references/recovery.md`.

## Compatibility and migration

Version 1.0.0 does not mutate an active 0.4.0 Stage. Existing active Stages fail closed and must finish with the installed 0.4.0 Skill or be explicitly abandoned by the operator. Migrate only between Stages: back up control artifacts, install 1.0.0, review and merge the new config keys, run `harness doctor`, review state/config together, and start the next Stage so the new baseline is captured. User files and existing Git history are preserved. `config_version` remains 1; unknown versions fail closed.

Mandatory model names from the research report are not adopted because product labels and availability drift. Optional per-role model routing accepts deployment aliases in `[agent_runtime.models]`; omission uses the configured runtime default. Legacy `[codex]` configuration is rejected during active work and may be migrated explicitly only between Stages. Commit-per-Slice is not adopted because exact candidate identity provides revision binding while preserving Stage-level commit policy.

## Read on demand

- Command order and Kanban boundary: `references/workflow.md`
- Executable states and transitions: `references/state-machine.md`
- Recovery and invalidation: `references/recovery.md`
- Control/source/evidence/reasoning layers: `references/architecture.md`
- Agent handoff contracts: `references/contracts/*.schema.json`
- Executable runtime contract: `references/adapter-contract.md`
- Gate contract: `schemas/quality-gates.schema.json`

## Verification

Run:

```text
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_harness.py
scripts/harness --version
scripts/harness doctor
```

For workflow changes, exercise `init`, `start-stage`, and every worker role with `--dry-run` in a disposable committed repository. No `__pycache__` may remain.
