# Progress

## Completed: context-policy-v1

Scope: define when to resume an exact thread, start fresh, or require independent context without adding runtime heuristics or orchestration.

Acceptance:

- Repository artifacts are durable memory; Codex threads are temporary task-local working memory.
- Same-task debugging resumes; new work and independence boundaries start fresh.
- Skills load selectively, single-agent execution stays default, and independent review stays bounded.
- Runtime code, process states, WIP=1, timeout handling, and evidence format are unchanged.

Verification evidence (2026-08-16):

- `./init.sh`: 7 runtime unit tests and 2 integration tests passed.
- Skill quick validation: `skill/` and the lifecycle compatibility router both valid.
- `git diff --check`: passed.
- Size: 16 files / 882 LOC; production runtime remains 277 LOC.

## Completed: thin-runtime-v2

Scope: retain only bounded Codex CLI invocation, WIP=1, timeout/process lifecycle, runtime evidence, and exact-session recovery.

Acceptance:

- Addy owns engineering methodology.
- `harness-creator` owns generic repository harness and lifecycle.
- No mandatory role pipeline or task approval state machine remains.
- Runtime regression tests, Skill validation, shell syntax checks, and a real Codex CLI smoke pass.

Verification evidence (2026-08-16):

- `./init.sh`: 7 runtime unit tests and 2 integration tests passed.
- Skill quick validation: `skill/` and the lifecycle compatibility router both valid.
- `git diff --check`: passed.
- Real Codex CLI 0.147.0 run: `succeeded`, exact response `THIN_RUNTIME_SMOKE_OK`, thread and usage captured.
- Real exact-thread resume: `succeeded`, exact response `THIN_RUNTIME_RESUME_OK`, same thread ID retained.
- Size: 61 files / 16,623 LOC before; 16 files / 799 LOC after.
