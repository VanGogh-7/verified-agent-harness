# Trusted-local Harness hardening plan

Status: Verification pending on frozen candidate

## Scope

Harden the Hermes + Codex Harness, context plugin, launcher, and Group project
Harness configuration. Do not modify Group product code, enable background
Hermes writes, or claim safety for arbitrary hostile repositories.

Baseline source commit: `82aa75d540971264058c2aac491f35430562b8ad`.
Baseline launcher SHA-256: `05e04509f1839a0733207b41111dd0f70f61f97ce596a753e5f59c71f5dc0149`.
Baseline deployed skill script SHA-256: `3eae3da2c32dfd0a872748dd853f9345c0d21e2c3823d7f3116d688a9c7f1c0b`.
Installation paths are deployment targets only; all source, tests, and
documentation live in this independent repository.

## Invariants

- Project code executes as the current Unix user in trusted-local mode.
- Gate environments use temporary HOME and a credential/proxy-free allowlist.
- Every runtime log is a fresh single-link owner-owned regular inode.
- Artifact readers reject symlinks, hardlinks, FIFOs, devices, and sockets.
- Worker state and checkpoints bind Stage, Slice, attempt, role, generation,
  owner identity, and worker identity; state updates use a revision CAS.
- Worker and Gate timeouts reclaim descendants and never leave active states.
- `status` is read-only; active capability probes require `doctor --capabilities`.
- Gate tiers are fast, slice, and stage; Group stage uses `./scripts/verify full`.

## Verification

- Harness unit/integration tests cover every named attack and recovery case.
- Default and capability doctor modes are run from Group.
- Group `./scripts/verify full` and `git diff --check` pass.
- A final clean source commit is frozen, deployed, hashed, and reviewed by a
  fresh read-only Codex Reviewer before any real-Stage readiness statement.

## Decision log

- 2026-08-03: cgroup v2 is optional defense in depth. Process groups plus a
  child subreaper are the portable required lifecycle mechanism on this host.
- 2026-08-03: line-buffered streaming redaction intentionally trades bounded
  memory for the rule that an unterminated possible secret suffix is never emitted.
