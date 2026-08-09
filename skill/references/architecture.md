# Architecture layers

## Trusted control plane

`.git/harness-control/` contains typed canonical state, configuration, protected baseline, generation checkpoint, and accepted evidence. Atomic writes, revision checks, and the single-writer lock protect transitions. Kanban is outside this trust boundary.

## Source state

Git HEAD is the stable base. A candidate may be uncommitted; `candidate_id` binds the base to the exact credential-safe worktree fingerprint. Protected project files are separately snapshotted.

## Evidence

Implementer, deterministic gates, every policy-required assessor, and Verifier produce strict revision-bound JSON. Runtime files are diagnostic mirrors. Approval reads canonical evidence and recomputes current identity. `run-advisory` produces non-authoritative runtime records only: Explorer, Researcher, Test Triage, and Log Triage cannot satisfy an assessment, enter the Verifier join, approve, or force repair.

## Ephemeral reasoning

Prompts, worker logs, assessor reasoning, non-authoritative advisory output, notifications, and optional Kanban descriptions may help produce evidence but never approve work. Advisory output can be candidate-bound for current-context usefulness, but it is never copied to canonical evidence. These materials are replaceable and may be compressed or discarded according to recovery policy.
