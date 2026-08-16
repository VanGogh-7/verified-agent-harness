---
name: project-lifecycle-harness
description: Compatibility router for repositories that still reference the former lifecycle Skill. Use harness-creator for bootstrap, adoption, instructions, state, verification, handoff, repair, or lifecycle work.
---

# Lifecycle compatibility router

The former lifecycle implementation was removed because `harness-creator` owns this responsibility.

Use the installed `harness-creator` Skill. Do not run a second lifecycle state machine, create formal lifecycle reviewer roles, or install codex-harness runtime state as repository task state.

This shim exists only to keep existing installed Skill links and old `AGENTS.md` references intelligible. New repositories should reference `harness-creator` directly.
