#!/usr/bin/env python3
"""Small, inspectable Codex CLI process runner.

This module deliberately knows nothing about planning, reviews, task approval, or
repository verification policy.  It only controls and records Codex processes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid


VERSION = "2.0.0"
RUNTIME_DIR = ".codex-harness"
SANDBOXES = ("read-only", "workspace-write")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max", "ultra")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_root(workspace: Path) -> Path:
    return workspace / RUNTIME_DIR


@contextmanager
def exclusive_run(workspace: Path):
    root = runtime_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "run.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another codex-harness run is active (WIP limit is 1)") from exc
        recover_interrupted_runs(root)
        yield


def recover_interrupted_runs(root: Path) -> None:
    for path in (root / "runs").glob("*/metadata.json"):
        try:
            value = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if value.get("state") == "running":
            value["state"] = "interrupted"
            value["finished_at"] = utc_now()
            value["failure"] = "runner exited before recording a terminal result"
            atomic_json(path, value)


def make_run(workspace: Path, command: str, parent_run_id: str | None = None) -> tuple[Path, dict[str, object]]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8]
    directory = runtime_root(workspace) / "runs" / run_id
    directory.mkdir(parents=True)
    metadata: dict[str, object] = {
        "format": 1,
        "run_id": run_id,
        "command": command,
        "parent_run_id": parent_run_id,
        "workspace": str(workspace),
        "state": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "exit_code": None,
        "thread_id": None,
        "usage": None,
    }
    atomic_json(directory / "metadata.json", metadata)
    return directory, metadata


def extract_events(path: Path) -> tuple[str | None, object | None]:
    thread_id: str | None = None
    usage: object | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        if event.get("type") == "turn.completed" and event.get("usage") is not None:
            usage = event["usage"]
    return thread_id, usage


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def codex_argv(args: argparse.Namespace, output: Path, thread_id: str | None) -> list[str]:
    argv = [args.codex, "exec", "--sandbox", args.sandbox, "--json", "--color", "never",
            "-C", str(args.workspace), "--output-last-message", str(output)]
    if args.model:
        argv += ["--model", args.model]
    if args.reasoning_effort:
        argv += ["-c", f'model_reasoning_effort="{args.reasoning_effort}"']
    if args.schema:
        argv += ["--output-schema", str(args.schema)]
    if args.ephemeral:
        argv.append("--ephemeral")
    if thread_id:
        argv += ["resume", thread_id, "-"]
    else:
        argv.append("-")
    return argv


def execute(args: argparse.Namespace, thread_id: str | None = None,
            parent_run_id: str | None = None) -> int:
    workspace = args.workspace.resolve(strict=True)
    args.workspace = workspace
    prompt = args.prompt_file.resolve(strict=True)
    if args.schema:
        args.schema = args.schema.resolve(strict=True)
    with exclusive_run(workspace):
        directory, metadata = make_run(workspace, "resume" if thread_id else "run", parent_run_id)
        events = directory / "events.jsonl"
        stderr = directory / "stderr.log"
        final = directory / "final.md"
        argv = codex_argv(args, final, thread_id)
        metadata.update({
            "sandbox": args.sandbox,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "timeout_seconds": args.timeout,
            "schema": str(args.schema) if args.schema else None,
            "ephemeral": args.ephemeral,
        })
        atomic_json(directory / "metadata.json", metadata)
        timed_out = False
        try:
            with prompt.open("rb") as stdin, events.open("wb") as stdout, stderr.open("wb") as err:
                process = subprocess.Popen(argv, cwd=workspace, stdin=stdin, stdout=stdout, stderr=err,
                                           start_new_session=True)
                try:
                    exit_code = process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_group(process)
                    exit_code = 124
        except OSError as exc:
            exit_code = 127
            stderr.write_text(f"could not start Codex CLI: {type(exc).__name__}\n", encoding="utf-8")
        found_thread, usage = extract_events(events) if events.exists() else (None, None)
        metadata.update({
            "state": "timed_out" if timed_out else ("succeeded" if exit_code == 0 else "failed"),
            "finished_at": utc_now(),
            "exit_code": exit_code,
            "thread_id": found_thread or thread_id,
            "usage": usage,
        })
        atomic_json(directory / "metadata.json", metadata)
        print(json.dumps(metadata, sort_keys=True))
        return exit_code


def latest_run(workspace: Path) -> Path:
    runs = sorted((runtime_root(workspace) / "runs").glob("*/metadata.json"))
    if not runs:
        raise RuntimeError("no recorded codex-harness runs")
    return runs[-1]


def command_resume(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve(strict=True)
    parent_path = runtime_root(workspace) / "runs" / args.run_id / "metadata.json"
    parent = read_json(parent_path)
    if parent.get("ephemeral") is True:
        raise RuntimeError(f"run {args.run_id} was ephemeral and cannot be resumed")
    thread_id = parent.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError(f"run {args.run_id} has no resumable Codex thread ID")
    return execute(args, thread_id, args.run_id)


def command_status(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve(strict=True)
    path = (runtime_root(workspace) / "runs" / args.run_id / "metadata.json"
            if args.run_id else latest_run(workspace))
    print(json.dumps(read_json(path), sort_keys=True))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    probe = subprocess.run([args.codex, "--version"], text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=10, check=False)
    print(json.dumps({"available": probe.returncode == 0, "version": probe.stdout.strip(),
                      "runtime_version": VERSION}, sort_keys=True))
    return 0 if probe.returncode == 0 else 1


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Thin Codex runtime harness")
    value.add_argument("--version", action="version", version=VERSION)
    sub = value.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--codex", default="codex")
    doctor.set_defaults(function=command_doctor)

    status = sub.add_parser("status")
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    status.add_argument("--run-id")
    status.set_defaults(function=command_status)

    for name in ("run", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--prompt-file", type=Path, required=True)
        command.add_argument("--timeout", type=positive_timeout, default=1800)
        command.add_argument("--sandbox", choices=SANDBOXES, default="workspace-write")
        command.add_argument("--model")
        command.add_argument("--reasoning-effort", choices=EFFORTS)
        command.add_argument("--schema", type=Path)
        command.add_argument("--ephemeral", action="store_true")
        command.add_argument("--codex", default="codex", help=argparse.SUPPRESS)
        if name == "resume":
            command.add_argument("--run-id", required=True)
            command.set_defaults(function=command_resume)
        else:
            command.set_defaults(function=execute)
    return value


def positive_timeout(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        return args.function(args)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"codex-harness: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
