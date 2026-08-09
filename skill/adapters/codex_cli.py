#!/usr/bin/env python3
"""Reference adapter from the verified-agent-harness contract to Codex CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


CONTRACT_VERSION = "1.1"
ROLE_ACCESS = {
    "Implementer": "workspace-write",
    "Correctness Reviewer": "read-only",
    "Tester": "read-only",
    "Security Reviewer": "read-only",
    "Verifier": "read-only",
    "Explorer": "read-only",
    "Researcher": "read-only",
    "Test Triage": "read-only",
    "Log Triage": "read-only",
    "Architecture Analyst": "read-only",
    "Independent Auditor": "read-only",
    "Final Lifecycle Reviewer": "read-only",
}
ROLE_DEFAULTS = {
    "Implementer": ("gpt-5.6-terra", "xhigh"),
    "Correctness Reviewer": ("gpt-5.6-sol", "medium"),
    "Tester": ("gpt-5.6-terra", "xhigh"),
    "Security Reviewer": ("gpt-5.6-sol", "medium"),
    "Verifier": ("gpt-5.6-sol", "medium"),
    "Explorer": ("gpt-5.6-luna", "xhigh"),
    "Researcher": ("gpt-5.6-terra", "xhigh"),
    "Test Triage": ("gpt-5.6-luna", "xhigh"),
    "Log Triage": ("gpt-5.6-luna", "xhigh"),
    "Architecture Analyst": ("gpt-5.6-terra", "xhigh"),
    "Independent Auditor": ("gpt-5.6-sol", "medium"),
    "Final Lifecycle Reviewer": ("gpt-5.6-sol", "medium"),
}
MODEL_DEFAULT_EFFORT = {
    "gpt-5.6-sol": "medium",
    "gpt-5.6-terra": "xhigh",
    "gpt-5.6-luna": "xhigh",
}
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max", "ultra"})


def describe() -> int:
    executable = "codex"
    resolved = shutil.which(executable) if "/" not in executable else executable
    print(json.dumps({
        "contract_version": CONTRACT_VERSION,
        "adapter": "codex-cli-reference",
        "available": bool(resolved and Path(resolved).is_file()),
        "capabilities": {
            "structured_output": True,
            "ephemeral": True,
            "role_access": ROLE_ACCESS,
            "role_defaults": ROLE_DEFAULTS,
            "reasoning_efforts": sorted(REASONING_EFFORTS),
        },
    }, sort_keys=True))
    return 0 if resolved and Path(resolved).is_file() else 1


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Codex CLI reference adapter")
    value.add_argument("--role", required=True, choices=tuple(ROLE_ACCESS))
    value.add_argument("--access", required=True, choices=("workspace-write", "read-only"))
    value.add_argument("--workdir", required=True)
    value.add_argument("--prompt", required=True)
    value.add_argument("--schema", required=True)
    value.add_argument("--output", required=True)
    value.add_argument("--model-alias", required=True)
    value.add_argument("--reasoning-effort", required=True)
    value.add_argument("--ephemeral", required=True, choices=("true",))
    return value


def effective_routing(role: str, model_alias: str, reasoning_effort: str) -> tuple[str, str]:
    """Return the reference policy's explicit Codex model and reasoning request."""
    if role not in ROLE_DEFAULTS:
        raise ValueError(f"unsupported Codex adapter role: {role}")
    model = model_alias or ROLE_DEFAULTS[role][0]
    if model not in MODEL_DEFAULT_EFFORT:
        raise ValueError(f"unsupported Codex model alias: {model}")
    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"unsupported Codex reasoning effort: {reasoning_effort}")
    return model, reasoning_effort or MODEL_DEFAULT_EFFORT[model]


def main() -> int:
    if sys.argv[1:] == ["--describe"]:
        return describe()
    args = parser().parse_args()
    if ROLE_ACCESS[args.role] != args.access:
        raise SystemExit(f"access {args.access!r} is invalid for {args.role}")
    workdir = Path(args.workdir).resolve(strict=True)
    prompt = Path(args.prompt).resolve(strict=True)
    schema = Path(args.schema).resolve(strict=True)
    output = Path(args.output).absolute()
    executable = "codex"
    try:
        model, reasoning_effort = effective_routing(
            args.role, args.model_alias, args.reasoning_effort,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    argv = [
        executable, "exec", "--sandbox", args.access, "--ephemeral",
        "--model", model, "-c", f'model_reasoning_effort="{reasoning_effort}"',
    ]
    argv += [
        "--output-schema", str(schema), "--output-last-message", str(output),
        "--color", "never", "-C", str(workdir), "-",
    ]
    try:
        with prompt.open("r", encoding="utf-8") as stdin:
            return subprocess.run(argv, cwd=workdir, stdin=stdin, check=False).returncode
    except OSError as exc:
        print(f"reference adapter could not start Codex CLI: {type(exc).__name__}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
