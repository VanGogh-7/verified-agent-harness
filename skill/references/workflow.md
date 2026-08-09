# Workflow commands

## VERIFIED

```text
harness init
harness start-stage --workflow VERIFIED --stage S1 --title "..." --slice S1.1 --plan-file docs/plan.md
harness run-implementer
harness run-gates --level slice
harness run-reviewer
harness run-tester
harness run-verifier
harness approve-slice --next-slice S1.2 --plan-file docs/next.md
```

Use `--level stage` and `approve-slice --complete-stage` for the final Slice. Each real worker should run through the configured Orchestrator's durable process facility; bind the external session with `status --json` then `bind-session`.

## SECURITY

```text
harness start-stage --workflow SECURITY ...
harness run-implementer
harness run-gates --level slice
harness run-reviewer
harness run-tester
harness run-security-reviewer
harness run-verifier
```

Security Reviewer is rejected outside SECURITY and required inside it. Read-only roles may execute serially on constrained hosts; this is logical fan-out, not mandatory concurrency.

## Optional advisory

```text
harness run-advisory --role explorer
harness run-advisory --role researcher --dry-run
```

`run-advisory` is the one generic command for Explorer, Researcher, Test Triage, and Log Triage. Each is read-only and non-authoritative: it may inspect and return strict structured advice, but cannot mutate source, satisfy a required assessment, enter the Verifier join, approve, or force repair. The command refuses while a worker or gate holds the single-writer lock. With an active Stage, output is bound to the current base and candidate only as runtime material; it is not trusted evidence.

## FAST, DAG, and LONG_RUNNING

FAST uses one writer plus direct deterministic verification and should not initialize the Stage engine solely for trivial work. DAG uses isolated worktrees and optional durable tasks for genuinely independent writers. LONG_RUNNING uses durable tasks, event-driven waiting, and explicit completion contracts. In DAG and LONG_RUNNING, every candidate still passes the verified inner Harness before integration. Outer task metadata is coordination state, never the trusted approval boundary.

## Evidence and resource rules

Before every heavy action, run `harness status --json` and perform the host resource preflight. Never overlap heavy processes. Worker environments are minimal and gates use a temporary HOME; neither is a hostile-repository sandbox. Do not read `.env` files. Runtime reports are mirrors; approval uses `.git/harness-control/` and recomputes `candidate_id`.

Dry-run all Stage and advisory roles without business mutation:

```text
harness run-implementer --dry-run
harness run-reviewer --dry-run
harness run-tester --dry-run
harness run-security-reviewer --dry-run
harness run-verifier --dry-run
harness run-advisory --role log_triage --dry-run
```
