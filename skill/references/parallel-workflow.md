# Hermes-led parallel workflow

## Goal

Use Hermes-led decomposition to reduce wall-clock time without turning the Harness into a free-form swarm. Hermes first decides whether a task is genuinely decomposable, freezes shared interfaces, and then coordinates independent writers. Serial execution remains the default when write scopes or acceptance criteria cannot be separated safely.

This Skill supplies strict validation and safety primitives; it does not claim to be a distributed scheduler. Hermes remains the outer controller. It may fan out only through execution facilities that expose complete worker containment and verifiable lifecycle control. `delegate_task` children without cgroup-wide freeze/thaw are suitable for bounded read-only analysis, but not for memory-preemptible parallel writers. If safe pause/resume is unavailable, Hermes must reduce admission, stop launching new work, and fall back to serial execution.

## Planning contract

Before launching writable work, Hermes creates a `parallel-plan.schema.json` artifact bound to the current Git `base_sha`. The plan records:

- `decision`: `serial` or `parallel` and a concrete rationale;
- a dependency DAG whose ready nodes form execution waves;
- exact project-relative `write_paths` for every task;
- acceptance checks and resource class for every task;
- shared contracts, their single owning task, and contract acceptance criteria;
- deterministic integration order and post-integration gates.

Validation fails closed on duplicate IDs, dependency cycles, unknown dependencies or contracts, overlapping task write scopes, control-path writes, contract paths not owned by the designated contract task, or an integration order that violates dependencies.

## Shared contract freeze

A shared contract freeze precedes dependent implementation. Public APIs, schemas, types, file formats, protocol behavior, and test fixtures that cross task boundaries have exactly one owner. Every contract-owner task is dependency-free, owns only frozen contract paths, has passed the ordinary inner Harness, and is already committed in the frozen `base_sha`. Dependent tasks are created from that exact revision; they may consume the contract but must not modify it. The atomic canonical `run.json` bundle binds the plan, base, and contract blob digests. Any drift fails closed.

## Parallel execution

Each writable task receives one isolated Git worktree and one writer. No two tasks may own overlapping path prefixes. The executable controller admits a worker only when every declared dependency is frozen in the base or has a Git-validated lane. Hermes launches only those ready tasks in the current DAG wave, supplies the frozen contracts and acceptance criteria, and records each task's base revision and resulting commit. A task that discovers an undeclared dependency stops and reports it; Hermes updates and revalidates the plan before continuing.

Read-heavy discovery may run in parallel without worktrees. Heavy builds, model runs, benchmarks, and package operations are serialized unless explicit host capacity permits safe overlap.

## Continuous memory guard

Parallel execution requires a continuous memory guard, not only a resource preflight. Hermes samples at least every five seconds while parallel workers are alive:

- `/proc/meminfo` `MemTotal` and `MemAvailable`;
- free swap capacity;
- Linux PSI values from `/proc/pressure/memory` (`some avg10` and `full avg10`);
- each worker's declared `light` or `heavy` resource class and containment identity.

The coordinator uses hysteresis to avoid rapid pause/resume oscillation:

- Critical pressure: available memory at or below the larger of 1 GiB or 8% of RAM, swap free at or below 512 MiB, `full avg10 >= 1`, or `some avg10 >= 10`. The coordinator must selectively pause heavy tasks first, then pause additional newest tasks if pressure persists.
- Elevated pressure: available memory at or below the larger of 2 GiB or 15% of RAM, `full avg10 >= 0.5`, or `some avg10 >= 5`. Pause one task and remeasure.
- Recovery: after three consecutive samples where available memory reaches the larger of 4 GiB or 30% of RAM, swap is absent or free exceeds 1 GiB, `full avg10 < 0.1`, and `some avg10 < 1`, resume exactly one paused task.

Each worker containment is created by the Harness, never supplied as an arbitrary caller path. Registration atomically binds a random creation token plus the exact parent and child cgroup device/inode identities to the run, task, and generation, and returns the new path. Hermes moves the writer into that cgroup, then calls `activate-parallel-worker`; activation succeeds only after the exact cgroup reports `populated 1`. Every canonical write and every freeze or thaw revalidates the path, token, parent identity, child identity, and controls. A lane can be recorded only if that same containment was activated and later reports `populated 0`; Git validity alone never permits forgetting a live worker. Recovery streak and admission are process-local decisions, not forgeable persisted fields. Pausing must target complete containment, never only the launcher PID or an arbitrary workers file. With no active workers Hermes safely switches to serial execution. An active uncontained worker enters `BLOCKED_UNCONTAINED` and must be drained before serial work proceeds. Unresolved elevated or critical pressure keeps `safe_to_continue=false`; a healthy sample is required before work is considered safe again. Memory pressure never changes acceptance, candidate identity, or evidence requirements.

## Integration and evidence

Hermes integrates completed task commits into a clean integration worktree in the validated order. Before each integration it checks that:

1. the task commit descends from its declared committed base;
2. changed paths derived from Git—not the worker's self-reported `changed_files`—are a subset of its approved `write_paths`;
3. shared contracts still match the frozen revision;
4. already integrated commits and the integration worktree have not drifted.

A merge conflict, unexpected changed path, contract drift, stale base, or failed task acceptance check stops integration. Hermes does not auto-resolve across ownership boundaries.

After integration, the result is one integration candidate. Run deterministic gates and one inner Harness validation on that complete candidate: required independent assessors, Verifier, and approval. Per-task reports are coordination evidence only and cannot approve the integrated result.

## When to stay serial

Keep execution serial when any of these holds:

- fewer than two ready tasks have disjoint write scopes;
- the public contract is still changing;
- tasks share mutable fixtures, migrations, generated files, ports, databases, GPUs, or exclusive test resources;
- integration cost or likely conflict exceeds saved execution time;
- the continuous memory guard cannot preserve safe headroom;
- the host cannot enforce complete worker containment for pause, resume, and cleanup.
