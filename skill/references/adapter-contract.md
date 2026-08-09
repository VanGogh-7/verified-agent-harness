# Executable agent adapter contract

The Harness invokes the configured `[agent_runtime].adapter_argv` directly as an
argv array. It never uses a shell. The configuration is trusted control-plane
state, is included in the protected baseline, and must not be editable by worker
roles.

Every adapter invocation appends these arguments in this exact order:

```text
--role <canonical role>
--access <workspace-write|read-only>
--workdir <absolute project path>
--prompt <absolute UTF-8 prompt path>
--schema <absolute JSON Schema path>
--output <absolute structured-output path>
--model-alias <deployment alias or empty string>
--ephemeral true
```

Canonical roles are `Implementer`, `Correctness Reviewer`, `Tester`, `Security
Reviewer`, and `Verifier`. Only Implementer is paired with `workspace-write`;
every other role is paired with `read-only`. An adapter must reject any other
pairing and must create the requested output according to the supplied schema.
The adapter receives the bounded worker environment and runs in the supplied
workdir. It must return the underlying agent runtime exit status.

The adapter is part of the trusted computing base. For `read-only`, it must map
the request to a provider-enforced read-only sandbox or an equivalent execution
boundary. The core recomputes candidate identity before accepting evidence, so a
write by a defective adapter fails closed and invalidates the handoff; that check
detects mutation but cannot prevent or undo it. Therefore an arbitrary executable
that merely accepts these arguments is not a supported adapter.

For doctor compatibility, an adapter must also accept `--describe` alone and
write one JSON object containing `{"contract_version":"1.0"}`. A replacement
runtime is supported only after its executable implements this contract and the
contract tests pass; command similarity or documentation claims are insufficient.

The portable `{skill_root}` token in configured argv elements expands to the
absolute installed Skill root without shell evaluation. See `adapters/README.md`
for the shipped reference integration.
