#!/usr/bin/env python3
"""Reference adapter from the verified-agent-harness contract to Codex CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


CONTRACT_VERSION = "1.0"
ROLE_ACCESS = {
    "Implementer": "workspace-write",
    "Correctness Reviewer": "read-only",
    "Tester": "read-only",
    "Security Reviewer": "read-only",
    "Verifier": "read-only",
}


def describe() -> int:
    executable = "codex"
    resolved = shutil.which(executable) if "/" not in executable else executable
    print(json.dumps({
        "contract_version": CONTRACT_VERSION,
        "adapter": "codex-cli-reference",
        "runtime_executable": resolved,
        "available": bool(resolved and Path(resolved).is_file()),
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
    value.add_argument("--ephemeral", required=True, choices=("true",))
    return value


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
    argv = [
        executable, "exec", "--sandbox", args.access, "--ephemeral",
    ]
    if args.model_alias:
        argv += ["--model", args.model_alias]
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
