# Hermes + Codex Harness

This repository is the source of truth for the local Hermes + Codex Harness.
The directories under `~/.hermes/` and the launcher under `~/.local/bin/` are
deployment targets only.

Version 0.5 is intentionally limited to trusted-local use. Project commands run
as the current Unix user. Process-tree cleanup, artifact checks, credential-safe
Gate environments, and Codex sandbox modes reduce accidental damage; they do not
make arbitrary hostile repositories safe to execute.

## Layout

- `skill/`: Hermes skill, CLI, schemas, templates, and operational references.
- `lifecycle-skill/`: separate greenfield, brownfield, and Harness-operation router.
- `plugin/`: read-only Hermes context-status plugin.
- `bin/harness`: installed launcher.
- `projects/Group/config.toml`: frozen Group deployment configuration.
- `tests/`: offline Harness security and lifecycle tests.
- `scripts/deploy`: clean-commit-only deployment to the configured targets.

## Verification and deployment

```bash
python3 -m py_compile skill/scripts/harness plugin/__init__.py scripts/configure-hermes scripts/deploy_transaction.py tests/test_harness.py tests/test_lifecycle.py tests/test_deployment.py
PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v
bash -n scripts/deploy bin/harness
git diff --check
```

Lifecycle entry uses the small read/write boundary below. `detect` and `assess`
are read-only; `bootstrap` and `adopt` require explicit approval; `activate`
requires a completed baseline Stage plus a passing independent read-only report.

Adoption rollback is a trusted-local Linux operation. This mode supports
serialization among cooperating Harness instances through the Harness lock,
crash recovery, interrupted operations, stale journals, malformed Agent
artifacts, and accidental local failures. It does not protect against a
malicious or externally coordinated same-UID process changing
`.git/harness-control` during a critical section, malicious repositories, or a
hostile administrator, kernel, or filesystem.

Rollback globally prevalidates the journal, backups, and target identities,
then uses `renameat2` atomic exchange/quarantine operations and refuses
rollback when the kernel or target filesystem cannot provide those guarantees;
it never downgrades to a check-then-path-write fallback.
`ADOPTION_ROLLED_BACK` means project state is restored and every displaced
object is durably accounted for in the generation-bound control-plane archive.
Archive detachment is a separate explicit operation and does not determine
rollback success.

`GC_DETACHED` is the terminal rollback-GC event. It records that the
journal-bound active archive root was atomically renamed into its
generation-bound `rollback-retained/` namespace, both parent directories were
fsynced, the moved identity and complete inventory were verified immediately,
the restored project state remained valid, and the event and bindings were
durably journaled. The filesystem rename and directory fsyncs are the namespace
commit point; the Harness does not claim atomicity between that operation and
the separate journal update. A prepared intent supports crash reconciliation.

`GC_DETACHED` is historical evidence, not a promise that a retained pathname
will permanently resolve to the same inode. `status` and `doctor` report later
retained identity or inventory changes as `RETAINED_ARCHIVE_DRIFT` without
rewriting `ADOPTION_ROLLED_BACK` or `GC_DETACHED`, adopting a replacement, or
deleting anything. An unrelated object later created at the former active
pathname is reported as `UNBOUND_ACTIVE_ARCHIVE_PRESENT`, remains untouched,
and has no authority over the retained record. Physical purge is optional
trusted-local storage maintenance outside rollback correctness and is currently
left to the operator; no automated Harness workflow invokes it.

```bash
harness detect --json
harness bootstrap --brief /path/to/PROJECT_BRIEF.md --dry-run
harness bootstrap --brief /path/to/PROJECT_BRIEF.md --approved --approved-plan-hash SHA256
harness assess --json
harness adopt --report HARNESS_ADOPTION_REPORT.md --analyst-report /tmp/analyst.json --auditor-report /tmp/auditor.json --dry-run
harness adopt --report HARNESS_ADOPTION_REPORT.md --analyst-report /tmp/analyst.json --auditor-report /tmp/auditor.json --approved --approved-plan-hash SHA256
harness adopt --resume
harness adopt --rollback
harness rollback-gc
harness activate --final-review /tmp/final-review.json
harness activate --resume
```

Deployment refuses a dirty source tree, exports only tracked files from the
frozen `HEAD`, and makes the skill/plugin targets exact (including stale-file
removal). It validates the complete staged payload, backs up every installed
target, performs controlled replacements, updates Hermes configuration, runs a
read-only post-deploy doctor, recomputes the actual installed manifest, rejects
post-doctor drift or generated files, and rolls back on any failure. It disables automatic
memory/profile writes, Skill creation nudges, and Curator mutation through
supported Hermes 0.19.1 keys while preserving manual Skill loading. Commit and
freeze the exact tested source first, then run `scripts/deploy`. In Group, run `harness sync-config`
between Stages to explicitly import the deployed project config into the
Git-metadata control plane.
