# Workflow

## Project bootstrap

From any directory inside the target Git repository:

```text
harness doctor
harness init
harness status
```

`init` discovers the Git root, existing context/architecture/readme/CI/build files, and known language manifests. It creates only `.harness/config.toml`, `state.json`, `PROJECT_STATE.md`, `CURRENT_STAGE.md`, and `runtime/`. It appends `/.harness/runtime/` to `.gitignore` without replacing existing content. Existing Harness files are not overwritten.

The CLI also keeps active control metadata under `.git/harness-control/`.
Canonical state, configuration, current-Stage text, state lock, and validated
implementation/gate/review evidence live
there; `.harness/` contains project-facing mirrors and runtime handoffs. Codex
`workspace-write` cannot alter Git metadata, so an Implementer cannot replace
gate commands, Reviewer isolation, or approval state. Configuration changes
are imported only by `start-stage` from a between-Stage state.

Before launching a worker, the Linux CLI creates and durably records a dedicated
cgroup v2, enters it before executing Codex, enables `PR_SET_CHILD_SUBREAPER`,
and starts a new process group. Before Codex starts, private user, mount, cgroup,
and PID namespaces cover `/sys/fs/cgroup`, `/proc`, and the user runtime directory;
all capabilities are dropped with no-new-privileges. The worker therefore cannot
migrate itself through delegated cgroup files or the user systemd socket. Harness
empties the cgroup with `cgroup.kill` and drains adopted descendants before
validating the handoff. Because the cgroup outlives the CLI, `recover` can kill
the complete containment after an orchestrator crash; recovery never accepts a
handoff first.

Review generated command arrays. They execute directly without a shell. For projects with bespoke entrypoints, edit only command arrays and project facts; never put pipelines, redirections, interpolation, or secret values in them.

## Stage lifecycle

1. Discuss and obtain explicit approval.
2. Put the approved plan in a temporary/plain Markdown file.
3. Run:

```text
harness start-stage --stage S1 --title "..." --slice S1.1 --plan-file docs/approved-plan.md
```

4. Start Implementer through a background Hermes terminal call:

```text
harness run-implementer
```

The Harness increments attempt, writes prompt/command/checkpoint atomically, starts Codex with `workspace-write`, `--ephemeral`, `--output-schema`, and `--output-last-message`, and streams combined stdout/stderr through credential-shaped redaction to the mode-0600 `.harness/runtime/implementer.log`. Its terminal output is one concise status line.

5. On completion, inspect `implementation.json` and concise Git evidence; do not inspect the full log unless the handoff is missing or a bounded error requires deliberate diagnosis.
6. Run `harness run-gates`. Each configured array is executed without a shell and gets a separate runtime log. At least one substantive gate must run; an all-skipped configuration fails closed for manual project configuration. The compact result is `quality-gates.json`.
7. Start `harness run-reviewer` in the background. Reviewer uses `read-only`, ephemeral mode, a review schema, and a separate log.
8. Read `review.json`. If changes are required, the state returns to `CHANGES_REQUIRED`; launch A again. Otherwise:

```text
harness approve-slice --next-slice S1.2 --plan-file docs/approved-slice-S1.2.md
# or, for the final Slice:
harness approve-slice --complete-stage
```

## Evidence boundary

Hermes normally reads only:

- `.harness/state.json`
- `.harness/runtime/implementation.json`
- `.harness/runtime/quality-gates.json`
- `.harness/runtime/review.json`
- concise `git status`, `git diff --stat`, `git diff --check`, and focused diff sections

Never ingest cumulative logs with `process(log)`. Worker logs remain on disk for explicit diagnostics. Error summaries are bounded by `error_excerpt_limit` and obvious credential-shaped text is redacted.

The runtime reports above are diagnostic mirrors. After validation, Harness
copies each report into `.git/harness-control/`; `approve-slice` reads only
those canonical copies. Runtime replacement cannot forge approval evidence.

`run-implementer --dry-run` and `run-reviewer --dry-run` write only `*-dry-run.*` command/prompt/isolation artifacts. They never change `state.json`, increment attempts, create canonical evidence, or satisfy approval prerequisites. Codex workers receive a minimal non-secret environment; authentication should use Codex's protected home rather than exported API-key variables.

## Upgrade

Replace/update the global `codex-harness` Skill directory and launcher, then run `harness doctor` in each project. Existing project state is retained. If `config_version` changes, migration must be explicit; the CLI fails closed rather than guessing. Review the generated template and merge new optional keys into existing project config.
