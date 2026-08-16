# Repository rules

- This project is a thin Codex CLI runtime Skill, not a software engineering framework.
- Addy Skills own engineering methodology. `harness-creator` owns generic repository harness and lifecycle guidance.
- Keep WIP at one. Prefer deletion and explicit code over new abstractions.
- Do not add task approval, candidate identity, formal worker roles, review loops, Git workflow, or generic verification policy to `codex-harness`.
- Production behavior changes require a regression test in `skill/scripts/test_runtime.py`.
- Verification entrypoint: `./init.sh`.
- Do not commit, push, tag, publish, or release without explicit user authorization.
