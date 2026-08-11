# Codex CLI reference adapter

`codex_cli.py` is optional/reference integration material. It translates the
provider-neutral executable adapter contract into direct `codex exec` argv while
preserving the current trusted-local behavior: ephemeral execution, structured
output schema, last-message output, explicit workdir, model/effort routing, and
the exact `workspace-write` or `read-only` sandbox selected by the core. It
never invokes a shell.

The reference adapter resolves `codex` from the bounded worker `PATH`. A deployment
that needs another executable must configure a different adapter argv or provide a
separate reviewed wrapper; provider-specific overrides are not implicit core state.

## Reference routing policy

When `[agent_runtime.models]` omits a role, this adapter uses the following
explicit model/effort pairs. An explicit valid per-role effort overrides the
derived effort. If a configured model is `Sol`, its omitted effort is `medium`;
if it is `Terra` or `Luna`, its omitted effort is `xhigh`.

| Role | Default |
|---|---|
| Implementer | Terra/ultra |
| Correctness Reviewer | Sol/medium |
| Tester | Terra/xhigh |
| Security Reviewer | Sol/medium |
| Verifier | Sol/medium |
| Explorer | Luna/xhigh |
| Researcher | Terra/xhigh |
| Test Triage | Luna/xhigh |
| Log Triage | Luna/xhigh |
| Architecture Analyst | Terra/xhigh |
| Independent Auditor | Sol/medium |
| Final Lifecycle Reviewer | Sol/medium |

The bundled Group configuration explicitly routes the Implementer at `ultra` effort because it is the only writer and must reason through implementation, debugging, and repair paths. Other Terra/Luna roles retain the adapter's `xhigh` policy unless explicitly overridden.

Explorer, Researcher, Test Triage, and Log Triage remain read-only and
non-authoritative. Their `run-advisory` output is never Stage approval evidence.
The three lifecycle labels share this adapter catalog only; lifecycle state stays
outside the Stage engine.
