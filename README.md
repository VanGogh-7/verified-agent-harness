# Hermes + Codex Harness

This repository is the source of truth for the local Hermes + Codex Harness.
The directories under `~/.hermes/` and the launcher under `~/.local/bin/` are
deployment targets only.

Version 0.2 is intentionally limited to trusted-local use. Project commands run
as the current Unix user. Process-tree cleanup, artifact checks, credential-safe
Gate environments, and Codex sandbox modes reduce accidental damage; they do not
make arbitrary hostile repositories safe to execute.

## Layout

- `skill/`: Hermes skill, CLI, schemas, templates, and operational references.
- `plugin/`: read-only Hermes context-status plugin.
- `bin/harness`: installed launcher.
- `projects/Group/config.toml`: frozen Group deployment configuration.
- `tests/`: offline Harness security and lifecycle tests.
- `scripts/deploy`: clean-commit-only deployment to the configured targets.

## Verification and deployment

```bash
python3 -m py_compile skill/scripts/harness plugin/__init__.py tests/test_harness.py
PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v
bash -n scripts/deploy bin/harness
git diff --check
```

Deployment refuses a dirty source tree, exports only tracked files from the
frozen `HEAD`, and makes the skill/plugin targets exact (including stale-file
removal). Commit and freeze the exact tested source first, then run `scripts/deploy`. In Group, run `harness sync-config`
between Stages to explicitly import the deployed project config into the
Git-metadata control plane.
