---
name: project-lifecycle-harness
description: Use when routing a project through greenfield bootstrap, brownfield Harness adoption, compatible Harness operation, or explicit Harness migration.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [project-lifecycle, bootstrap, adoption, harness]
    related_skills: [verified-agent-harness]
---

# Project Lifecycle Harness

## Boundary and language policy

Own project entry and readiness only. Do not absorb or duplicate the `verified-agent-harness` Stage/Slice engine. The installed repository `bin/harness` is the sole command router: it sends `detect`, `assess`, `bootstrap`, `adopt`, `activate`, and `rollback-gc` here, while the Stage Skill parser does not expose those commands. Once the lifecycle state is `HARNESS_READY`, delegate execution to `verified-agent-harness`.

Detect the current human operator's language from the active conversation. Use it for discussion, progress, approval, checkpoint, and completion messages in that conversation only. Ask architecture questions progressively, usually one focused group at a time. Do not proactively correct the operator's grammar, spelling, or wording. Re-detect language in a new conversation; never bind it to a project or user.

English is mandatory for Agent prompts and reports, architecture/adoption assessments, state and schemas, gate reports, repository engineering documents, plans, source comments, names, identifiers, configuration, Stage/Slice titles, Git messages, CI names, and templates. Preserve the meaning of approved non-English requirements while converting them into precise English artifacts. Never translate or rename paths, commands, code/API identifiers, Stage/Slice IDs, state values, Git references, package names, JSON keys, or error codes. Localized product strings may use required locales, but localization keys and engineering artifacts remain English.

## Route first

Run `harness detect --json`. Detection is read-only.

- `EMPTY_DIRECTORY` or `NEW_DIRECTORY` -> `GREENFIELD_BOOTSTRAP`.
- `GIT_WITHOUT_HARNESS` -> `BROWNFIELD_ADOPTION`.
- `COMPATIBLE_HARNESS` -> `HARNESS_OPERATION`.
- `DAMAGED_HARNESS` -> stop and repair or restore explicitly.
- `INCOMPATIBLE_HARNESS` -> explicit migration discussion; never overwrite or guess.
- `dirty_worktree=true` is an independent safety fact. Preserve user changes and obtain a decision before a write workflow if overlap is possible.

## GREENFIELD_BOOTSTRAP

1. Discuss the problem and target users, core use cases, MVP/non-goals, technical constraints, security/data boundaries, deployment, external services/tools, and acceptance criteria. Ask only the next architecture-relevant questions.
2. Write an English `PROJECT_BRIEF.md` draft from `templates/PROJECT_BRIEF.md` outside the target if the directory must remain pristine.
3. Obtain one explicit operator approval for the complete brief. Do not reinterpret a non-English approval.
4. Run `harness bootstrap --brief <path> --dry-run`, review every `CREATE`, `MODIFY`, `PRESERVE`, and `REJECT` entry, obtain approval, then run `harness bootstrap --brief <path> --approved --approved-plan-hash <sha256>`. This creates Git, standard English project docs, and the Harness; it does not mark readiness.
5. Define an English baseline Stage for the initial scaffold. Use `harness start-stage`, `run-implementer`, baseline gates, all policy-required independent assessors, `run-verifier`, and `approve-slice --complete-stage` exactly as required by `verified-agent-harness`. Implementer is workspace-write; assessors and Verifier are independent and read-only.
6. Run a separate Final Lifecycle Reviewer read-only lifecycle review returning the contract in `schemas/final-review.schema.json`, then `harness activate --final-review <path>`. If activation is interrupted after evidence persistence, use `harness activate --resume`; do not reread or replace the approved final report.

## BROWNFIELD_ADOPTION

1. Begin read-only. Run `harness assess --json` and inspect only relevant source, manifests, CI, tests, and engineering docs. Do not read secrets or `.env` contents.
2. Architecture Analyst uses `templates/analyst-prompt.md` and `schemas/analyst.schema.json`, read-only. An Independent Auditor uses `templates/auditor-prompt.md` and `schemas/auditor.schema.json`, read-only. Never let the active Orchestrator or implementation runtime approve its own Skill.
3. Validate both reports with `harness assess --analyst-report <path> --auditor-report <path> --json`. Missing or malformed reports stop adoption.
4. Synthesize an English `HARNESS_ADOPTION_REPORT.md` from `templates/HARNESS_ADOPTION_REPORT.md`. Ask the operator to approve detected architecture, protected paths, quality gates, context files, and adoption constraints.
5. Run `harness adopt --report <path> --analyst-report <path> --auditor-report <path> --dry-run`. Review the canonical mutation manifest and copy the exact generated approval block, including derived argv arrays, into the report. Only after approval run the same command with `--approved --approved-plan-hash <sha256>`. If interrupted, run `harness adopt --resume`; use `harness adopt --rollback` for CAS-protected restoration; never inject lifecycle state manually.
6. Use a narrowly scoped baseline Stage through `verified-agent-harness` for configuration/state validation only. Run baseline gates, every policy-required independent assessor, and the independent Verifier; complete the Stage, then run the final lifecycle review and `harness activate`.

During adoption never refactor business code, fix unrelated defects, upgrade dependencies, change public APIs, format the repository, or add product features. `harness assess` and `harness adopt --dry-run` are read-only with respect to business code.

## HARNESS_OPERATION

Run read-only `harness detect --json`, `harness doctor`, `harness status --json`, concise Git checks, and Worker/checkpoint checks. Then:

- `STAGE_DISCUSSION`: discuss and obtain approval for the next Stage.
- Active or interrupted work: follow `verified-agent-harness` recovery; never launch duplicates.
- `BLOCKED` or `HUMAN_CHECKPOINT`: request the exact operator decision.
- Incompatible version: use an explicit migration plan and approval.

Do not repeat a full architecture assessment unless the state, migration, or operator explicitly requires it.

## Completion

`HARNESS_READY` requires an approved brief/adoption report, initialized Harness, completed baseline Stage, passing baseline gates, independent read-only reviews, and a passing final lifecycle report. Report results in the active operator language while leaving all durable artifacts and Agent-to-Agent content in English.
