# Executable agent adapter contract

The Harness invokes the configured `[agent_runtime].adapter_argv` directly as an
argv array. It never uses a shell. The configuration is trusted control-plane
state, is included in the protected baseline, and must not be editable by worker
roles.

An installation may also configure `[agent_runtime].preflight_argv` as a trusted
argv array with `preflight_timeout_seconds` from 1 through 300. Immediately before
each worker launch, while holding the canonical state lock and before allocating
an attempt, owner, generation, or worker identity, the Harness runs this command
with the bounded worker environment. Standard input and both output streams are
discarded, so checkpoint state records only `not-configured` or a concise passed
result and never captures credentials. A timeout, spawn error, or nonzero status
fails closed without consuming a business attempt.

Every adapter invocation appends these arguments in this exact order:

```text
--role <canonical role>
--access <workspace-write|read-only>
--workdir <absolute project path>
--prompt <absolute UTF-8 prompt path>
--schema <absolute JSON Schema path>
--output <absolute structured-output path>
--model-alias <deployment alias or empty string>
--reasoning-effort <none|low|medium|high|xhigh|max|ultra or empty string>
--ephemeral true
```

Canonical Stage roles are `Implementer`, `Correctness Reviewer`, `Tester`,
`Security Reviewer`, and `Verifier`. Optional non-authoritative advisory roles
are `Explorer`, `Researcher`, `Test Triage`, and `Log Triage`; `run-advisory`
is their only Stage command. Lifecycle labels `Architecture Analyst`,
`Independent Auditor`, and `Final Lifecycle Reviewer` are read-only catalog
entries only and never become Stage evidence. Only Implementer is paired with
`workspace-write`; every other role is paired with `read-only`. An adapter must
reject any other pairing and must create the requested output according to the
supplied schema. It must also reject unknown reasoning values. The adapter
receives the bounded worker environment and runs in the supplied workdir. It
must return the underlying agent runtime exit status.

The adapter is part of the trusted computing base. For `read-only`, it must map
the request to a provider-enforced read-only sandbox or an equivalent execution
boundary. The core recomputes candidate identity before accepting evidence, so a
write by a defective adapter fails closed and invalidates the handoff; that check
detects mutation but cannot prevent or undo it. Therefore an arbitrary executable
that merely accepts these arguments is not a supported adapter.

For doctor compatibility, an adapter must also accept `--describe` alone and
write one secret-free JSON object containing `{"contract_version":"1.1"}` plus
`capabilities`. Capabilities must declare structured output, ephemeral support,
role access, and every accepted reasoning effort. A replacement runtime is
supported only after its executable implements this non-authoritative advisory
and routing contract and the contract tests pass; command similarity or
documentation claims are insufficient.

The portable `{skill_root}` token in configured argv elements expands to the
absolute installed Skill root without shell evaluation. See `adapters/README.md`
for the shipped reference integration.
