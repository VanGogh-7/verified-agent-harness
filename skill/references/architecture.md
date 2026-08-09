# Architecture layers

## Trusted control plane

`.git/harness-control/` contains typed canonical state, configuration, protected baseline, generation checkpoint, and accepted evidence. Atomic writes, revision checks, and the single-writer lock protect transitions. Kanban is outside this trust boundary.

## Source state

Git HEAD is the stable base. A candidate may be uncommitted; `candidate_id` binds the base to the exact credential-safe worktree fingerprint. Protected project files are separately snapshotted.

## Evidence

Implementer, deterministic gates, every policy-required assessor, and Verifier produce strict revision-bound JSON. Runtime files are diagnostic mirrors. Approval reads canonical evidence and recomputes current identity.

## Ephemeral reasoning

Prompts, worker logs, assessor reasoning, notifications, and optional Kanban descriptions may help produce evidence but never approve work. They are replaceable and may be compressed or discarded according to recovery policy.
