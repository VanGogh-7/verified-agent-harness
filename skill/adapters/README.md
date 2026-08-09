# Codex CLI reference adapter

`codex_cli.py` is optional/reference integration material. It translates the
provider-neutral executable adapter contract into `codex exec` arguments while
preserving the current trusted-local behavior: ephemeral execution, structured
output schema, last-message output, explicit workdir, model alias routing, and
the exact `workspace-write` or `read-only` sandbox selected by the core.

The reference adapter resolves `codex` from the bounded worker `PATH`. A deployment
that needs another executable must configure a different adapter argv or provide a
separate reviewed wrapper; provider-specific overrides are not implicit core state.
