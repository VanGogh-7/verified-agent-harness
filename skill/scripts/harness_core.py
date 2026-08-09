#!/usr/bin/env python3
"""Provider-neutral verified agent harness. Standard-library only."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from typing import Any, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

HARNESS_VERSION = "1.0.0"
CONFIG_VERSION = 1
LIFECYCLE_VERSION = 1
COMPATIBLE_HARNESS_VERSIONS = {"0.4.0", HARNESS_VERSION}
STATES = {
    "STAGE_DISCUSSION", "STAGE_APPROVED", "SLICE_READY", "IMPLEMENTING",
    "VALIDATING", "ASSESSING", "VERIFYING", "CHANGES_REQUIRED", "APPROVED", "BLOCKED",
    "HUMAN_CHECKPOINT", "STAGE_COMPLETED",
}
TRANSITIONS = {
    "STAGE_DISCUSSION": {"STAGE_APPROVED"},
    "STAGE_APPROVED": {"SLICE_READY", "BLOCKED"},
    "SLICE_READY": {"IMPLEMENTING", "BLOCKED", "HUMAN_CHECKPOINT"},
    "IMPLEMENTING": {"VALIDATING", "BLOCKED", "HUMAN_CHECKPOINT"},
    "VALIDATING": {"ASSESSING", "CHANGES_REQUIRED", "BLOCKED", "HUMAN_CHECKPOINT"},
    "ASSESSING": {"VERIFYING", "BLOCKED", "HUMAN_CHECKPOINT"},
    "VERIFYING": {"CHANGES_REQUIRED", "APPROVED", "BLOCKED", "HUMAN_CHECKPOINT"},
    "CHANGES_REQUIRED": {"IMPLEMENTING", "HUMAN_CHECKPOINT", "BLOCKED"},
    "APPROVED": {"SLICE_READY", "STAGE_COMPLETED", "CHANGES_REQUIRED"},
    "BLOCKED": {"SLICE_READY", "ASSESSING", "CHANGES_REQUIRED", "HUMAN_CHECKPOINT"},
    "HUMAN_CHECKPOINT": {"SLICE_READY", "CHANGES_REQUIRED", "BLOCKED"},
    "STAGE_COMPLETED": {"STAGE_APPROVED"},
}
REPORT_FILES = {
    "implementer": "implementation.json",
    "reviewer": "review.json",
    "tester": "test.json",
    "security_reviewer": "security-review.json",
    "verifier": "verification.json",
    "gates": "quality-gates.json",
}
SECRET_NAMES = re.compile(r"(?i)(api[_-]?key|token|password|secret|credential|authorization|cookie|database_url)")
SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"bearer\s+[^\s,;]+|"
    r"authorization\s*:\s*(?:basic|bearer)\s+[^\s,;]+|"
    r"[\"']authorization[\"']\s*:\s*[\"'](?:basic|bearer)\s+[^\"'\r\n]+[\"']|"
    r"[\"'](?:[A-Z0-9_]*(?:api[_-]?key|token|password|secret|credential|authorization|cookie)|database_url|accountkey|sig)[\"']\s*:\s*[\"'][^\"'\r\n]+[\"']|"
    r"(?:[A-Z0-9_]*(?:api[_-]?key|token|password|secret|credential|authorization|cookie)|database_url|accountkey|sig)\s*[:=]\s*[^\s,;]+|"
    r"(?:SharedAccessSignature\s+)?(?:sr|se|sp|sv|sig)=[^\s]+|"
    r"[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s@]+@\S+|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|"
    r"glpat-[0-9A-Za-z_-]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|"
    r"sk_live_[0-9A-Za-z]{16,}|"
    r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----"
    r")"
)


class HarnessError(RuntimeError):
    pass


def enable_child_subreaper() -> None:
    """Make orphaned worker descendants reparent to this dedicated CLI process."""
    if not sys.platform.startswith("linux"):
        raise HarnessError("secure worker lifecycle requires Linux PR_SET_CHILD_SUBREAPER")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise HarnessError(f"cannot enable child subreaper: errno={ctypes.get_errno()}")


def direct_child_pids(parent_pid: int) -> set[int]:
    children: set[int] = set()
    proc_root = pathlib.Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2:].split()
            if len(fields) > 1 and int(fields[1]) == parent_pid:
                children.add(int(entry.name))
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    return children


def current_cgroup_dir() -> pathlib.Path:
    raw = pathlib.Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
    relative = raw.split(":")[-1].lstrip("/")
    path = pathlib.Path("/sys/fs/cgroup") / relative
    if not (path / "cgroup.controllers").exists() and not (path / "cgroup.procs").exists():
        raise HarnessError("cgroup v2 is unavailable")
    return path


def validate_worker_cgroup(path: pathlib.Path) -> pathlib.Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(pathlib.Path("/sys/fs/cgroup")):
        raise HarnessError("worker cgroup is outside cgroup v2")
    if not resolved.name.startswith("verified-agent-harness-"):
        raise HarnessError("invalid worker cgroup identity")
    if not (resolved / "cgroup.procs").is_file() or not (resolved / "cgroup.kill").is_file():
        raise HarnessError("worker cgroup lacks required controls")
    return resolved


def create_worker_cgroup() -> pathlib.Path:
    parent = current_cgroup_dir()
    path = parent / f"verified-agent-harness-{os.getpid()}-{time.time_ns()}"
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise HarnessError(f"cannot create worker cgroup: {type(exc).__name__}") from exc
    return validate_worker_cgroup(path)


def destroy_worker_cgroup(path: pathlib.Path, kill: bool = True) -> None:
    if not path.exists():
        if (not path.is_absolute() or path.parent == path or
                not path.name.startswith("verified-agent-harness-") or
                not str(path).startswith("/sys/fs/cgroup/")):
            raise HarnessError("invalid missing worker cgroup identity")
        return
    cgroup = validate_worker_cgroup(path)
    if kill:
        try:
            (cgroup / "cgroup.kill").write_text("1", encoding="ascii")
        except OSError as exc:
            raise HarnessError(f"cannot kill worker cgroup: {type(exc).__name__}") from exc
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        events = (cgroup / "cgroup.events").read_text(encoding="ascii")
        if "populated 0" in events:
            break
        time.sleep(0.02)
    else:
        raise HarnessError("worker cgroup did not become empty")
    try:
        cgroup.rmdir()
    except OSError as exc:
        raise HarnessError(f"cannot remove worker cgroup: {type(exc).__name__}") from exc


def terminate_worker_tree(proc: subprocess.Popen[str], parent_waited: bool = False,
                          cgroup: pathlib.Path | None = None) -> int:
    """Terminate the worker process group and all subreaper-adopted descendants."""
    if not parent_waited and proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    if proc.poll() is None:
        proc.wait()
    rc = int(proc.returncode if proc.returncode is not None else 1)
    cgroup_error: HarnessError | None = None
    if cgroup is not None:
        try:
            destroy_worker_cgroup(cgroup, kill=True)
        except HarnessError as exc:
            cgroup_error = exc
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            adopted = direct_child_pids(os.getpid())
            if not adopted:
                break
            for pid in adopted:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, sig)
            time.sleep(0.02)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        adopted = direct_child_pids(os.getpid())
        for pid in adopted:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        while True:
            try:
                waited, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                if cgroup_error is not None:
                    raise cgroup_error
                return rc
            if waited == 0:
                break
        time.sleep(0.02)
    raise HarnessError("adopted worker descendants did not drain")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def concise(text: str, limit: int) -> str:
    clean = SECRET_VALUE.sub("[REDACTED]", text or "")
    clean = " ".join(clean.split())
    return clean[-limit:] if len(clean) > limit else clean


def read_log_tail(path: pathlib.Path, limit: int) -> str:
    """Read only a bounded tail before redaction; never load an unbounded worker log."""
    byte_limit = max(limit * 4, 4096)
    fd = open_single_link_regular(path, require_owner=False)
    with os.fdopen(fd, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - byte_limit))
        tail = handle.read(byte_limit).decode("utf-8", errors="replace")
    return concise(tail, limit)


def open_directory_nofollow(directory: pathlib.Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = -1
    try:
        if directory.name == "runtime" and directory.parent.name == ".harness":
            parent_fd = os.open(directory.parent, flags)
            fd = os.open(directory.name, flags, dir_fd=parent_fd)
        else:
            fd = os.open(directory, flags)
    except OSError as exc:
        raise HarnessError(f"cannot safely open runtime directory: {type(exc).__name__}") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise HarnessError("runtime path is not a directory")
    return fd


def validate_single_link_regular(fd: int, label: str, require_owner: bool = True) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise HarnessError(f"{label} is not a regular file")
    if info.st_nlink != 1:
        raise HarnessError(f"{label} must have exactly one hard link")
    if require_owner and info.st_uid != os.getuid():
        raise HarnessError(f"{label} is not owned by the current user")
    return info


def open_single_link_regular(path: pathlib.Path, require_owner: bool = True) -> int:
    """Open an artifact without following its final name and reject blocking/special files."""
    directory_fd = open_directory_nofollow(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path.name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise HarnessError(f"cannot safely open artifact {path.name}: {type(exc).__name__}") from exc
    finally:
        os.close(directory_fd)
    try:
        validate_single_link_regular(fd, f"artifact {path.name}", require_owner=require_owner)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_single_link_text(path: pathlib.Path, label: str,
                          max_bytes: int = 2 * 1024 * 1024,
                          require_owner: bool = True) -> str:
    """Read a bounded UTF-8 control artifact without following links."""
    fd = open_single_link_regular(path, require_owner=require_owner)
    try:
        info = os.fstat(fd)
        if info.st_size > max_bytes:
            raise HarnessError(f"{label} exceeds {max_bytes} bytes")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise HarnessError(f"{label} exceeds {max_bytes} bytes")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError(f"{label} is not valid UTF-8") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def atomic_runtime_write(path: pathlib.Path, data: str, mode: int = 0o600) -> None:
    directory_fd = open_directory_nofollow(path.parent)
    tmp_name = f".{path.name}.{os.getpid()}.{time.time_ns()}"
    fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp_name, flags, mode, dir_fd=directory_fd)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name, dir_fd=directory_fd)
        os.close(directory_fd)


def atomic_runtime_json(path: pathlib.Path, value: Any) -> None:
    atomic_runtime_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def runtime_unlink(path: pathlib.Path) -> None:
    directory_fd = open_directory_nofollow(path.parent)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path.name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def clear_directory_fd(directory_fd: int) -> None:
    flags = os.O_RDONLY | (os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def secure_log_file(path: pathlib.Path):
    """Replace a log name with a fresh, single-link, owner-only regular inode."""
    directory_fd = open_directory_nofollow(path.parent)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path.name, dir_fd=directory_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise HarnessError(f"cannot safely open runtime log {path.name}: {type(exc).__name__}") from exc
    finally:
        os.close(directory_fd)
    try:
        validate_single_link_regular(fd, f"runtime log {path.name}")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "w", encoding="utf-8")


def stream_redacted(pipe, log) -> None:
    # A line is withheld until its delimiter (or EOF), so a token split across
    # arbitrary read chunks can never leak an unconfirmed suffix.
    carry = ""
    while True:
        chunk = pipe.read(4096)
        if not chunk:
            break
        carry += chunk
        complete = carry.splitlines(keepends=True)
        carry = ""
        for item in complete:
            if item.endswith(("\n", "\r")):
                log.write(SECRET_VALUE.sub("[REDACTED]", item))
            else:
                carry = item
    if carry:
        log.write(SECRET_VALUE.sub("[REDACTED]", carry))
    log.flush()


def run_logged(argv: list[str], root: pathlib.Path, log_path: pathlib.Path, timeout: int,
               env: dict[str, str]) -> tuple[int, bool]:
    enable_child_subreaper()
    log = secure_log_file(log_path)
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, start_new_session=True)
        if proc.stdout is None:
            raise HarnessError("subprocess output pipe was not created")
        reader = threading.Thread(target=stream_redacted, args=(proc.stdout, log), daemon=True)
        reader.start()
        timed_out = False
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_worker_tree(proc)
            rc = 124
        else:
            terminate_worker_tree(proc, parent_waited=True)
        reader.join(timeout=5)
        if reader.is_alive():
            with contextlib.suppress(Exception):
                proc.stdout.close()
            reader.join(timeout=0.5)
        if reader.is_alive():
            raise HarnessError("subprocess log drain did not terminate")
        proc.stdout.close()
        os.fsync(log.fileno())
        return rc, timed_out
    except BaseException:
        if proc is not None:
            with contextlib.suppress(Exception):
                terminate_worker_tree(proc)
        raise
    finally:
        log.close()


def emit(command: str, outcome: str, **fields: Any) -> None:
    parts = [f"HARNESS {command} {outcome}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
    print(" ".join(parts))


def run_capture(argv: list[str], cwd: pathlib.Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, check=False, env=env)


def git_root(required: bool = True) -> pathlib.Path | None:
    cp = run_capture(["git", "rev-parse", "--show-toplevel"])
    if cp.returncode != 0:
        if required:
            raise HarnessError("current directory is not inside a Git repository")
        return None
    return pathlib.Path(cp.stdout.strip()).resolve()


def paths(root: pathlib.Path, allow_unsafe_runtime: bool = False) -> dict[str, pathlib.Path]:
    base = root / ".harness"
    runtime = base / "runtime"
    git_dir_result = run_capture(["git", "rev-parse", "--git-dir"], cwd=root)
    if git_dir_result.returncode != 0:
        raise HarnessError("cannot resolve Git metadata directory")
    git_dir = pathlib.Path(git_dir_result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    trusted = git_dir / "harness-control"
    for directory in (base, trusted):
        if directory.is_symlink():
            raise HarnessError(f"refusing symlinked Harness directory: {directory}")
    if runtime.is_symlink() and not allow_unsafe_runtime:
        raise HarnessError(f"refusing symlinked Harness directory: {runtime}")
    for name in ("config.toml", "state.json", "PROJECT_STATE.md", "CURRENT_STAGE.md"):
        candidate = base / name
        if candidate.is_symlink():
            raise HarnessError(f"refusing symlinked Harness control file: {candidate}")
    for name in ("config.toml", "state.json", "CURRENT_STAGE.md",
                 "orchestrator-checkpoint.json", "implementation.json",
                 "quality-gates.json", "review.json", "test.json",
                 "security-review.json", "verification.json", "state.lock"):
        candidate = trusted / name
        if candidate.is_symlink():
            raise HarnessError(f"refusing symlinked trusted control file: {candidate}")
    return {
        "base": base,
        "runtime": runtime,
        "config": base / "config.toml",
        "state": base / "state.json",
        "project": base / "PROJECT_STATE.md",
        "stage": base / "CURRENT_STAGE.md",
        "trusted": trusted,
        "trusted_config": trusted / "config.toml",
        "trusted_state": trusted / "state.json",
        "trusted_stage": trusted / "CURRENT_STAGE.md",
        "trusted_checkpoint": trusted / "orchestrator-checkpoint.json",
        "trusted_implementation": trusted / "implementation.json",
        "trusted_gates": trusted / "quality-gates.json",
        "trusted_review": trusted / "review.json",
        "trusted_test": trusted / "test.json",
        "trusted_security_review": trusted / "security-review.json",
        "trusted_verification": trusted / "verification.json",
        "lock": trusted / "state.lock",
        "checkpoint": runtime / "orchestrator-checkpoint.json",
    }


def fsync_dir(directory: pathlib.Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: pathlib.Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def atomic_json(path: pathlib.Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def state_lock(p: dict[str, pathlib.Path]):
    p["runtime"].mkdir(parents=True, exist_ok=True)
    p["trusted"].mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(p["lock"], flags, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_json(path: pathlib.Path, max_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    try:
        fd = open_single_link_regular(path, require_owner=False)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise HarnessError(f"JSON artifact exceeds {max_bytes} bytes: {path}")
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise HarnessError(f"missing {path}; run 'harness init'") from exc
    except OSError as exc:
        raise HarnessError(f"cannot safely read JSON artifact {path.name}: {type(exc).__name__}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"expected JSON object in {path}")
    return value


def load_state(p: dict[str, pathlib.Path], repair_mirror: bool = False) -> dict[str, Any]:
    source = p["trusted_state"] if p["trusted_state"].exists() else p["state"]
    state = load_json(source)
    if state.get("workflow_state") not in STATES:
        raise HarnessError("state.json contains an unknown workflow_state")
    mirror = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if source == p["trusted_state"] and repair_mirror:
        atomic_runtime_write(p["state"], mirror)
    return state


def save_state(p: dict[str, pathlib.Path], state: dict[str, Any], mirror: bool = True,
               owner_generation: str | None = None) -> None:
    expected_revision = int(state.get("state_revision", 0))
    if p["trusted_state"].exists():
        current = load_json(p["trusted_state"])
        if int(current.get("state_revision", 0)) != expected_revision:
            raise HarnessError("state CAS conflict; reload before writing")
        if owner_generation is not None:
            owner = current.get("owner") or {}
            if owner.get("generation") != owner_generation:
                raise HarnessError("stale Harness owner generation cannot update state")
    state["state_revision"] = expected_revision + 1
    state["updated_at"] = utc_now()
    atomic_json(p["trusted_state"], state)
    if mirror:
        atomic_runtime_json(p["state"], state)


def clear_trusted_evidence(p: dict[str, pathlib.Path]) -> None:
    for key in ("trusted_implementation", "trusted_gates", "trusted_review", "trusted_test",
                "trusted_security_review", "trusted_verification", "trusted_checkpoint"):
        with contextlib.suppress(FileNotFoundError):
            p[key].unlink()
    with contextlib.suppress(HarnessError):
        runtime_unlink(p["checkpoint"])
    for name in REPORT_FILES.values():
        with contextlib.suppress(HarnessError):
            runtime_unlink(p["runtime"] / name)


def record_transition(state: dict[str, Any], target: str, reason: str) -> None:
    current = state["workflow_state"]
    if target not in STATES or target not in TRANSITIONS.get(current, set()):
        raise HarnessError(f"illegal state transition: {current} -> {target}")
    state["workflow_state"] = target
    events = state.setdefault("recent_transitions", [])
    events.append({"from": current, "to": target, "reason": reason, "at": utc_now()})
    del events[:-12]


def transition(p: dict[str, pathlib.Path], state: dict[str, Any], target: str, reason: str) -> None:
    record_transition(state, target, reason)
    save_state(p, state)


def load_config(p: dict[str, pathlib.Path]) -> dict[str, Any]:
    if tomllib is None:
        raise HarnessError("Python 3.11+ is required (tomllib unavailable)")
    try:
        source = p["trusted_config"] if p["trusted_config"].exists() else p["config"]
        with source.open("rb") as handle:
            cfg = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise HarnessError("missing .harness/config.toml; run 'harness init'") from exc
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(f"invalid .harness/config.toml: {exc}") from exc
    if cfg.get("config_version") != CONFIG_VERSION:
        raise HarnessError(f"unsupported config_version={cfg.get('config_version')!r}")
    protected = cfg.get("protected_paths")
    if not isinstance(protected, list) or not all(isinstance(item, str) for item in protected):
        raise HarnessError("protected_paths must be an array of strings")
    for item in protected:
        validate_project_relative_path(item, "protected path", allow_glob=True)
    for required_path in (".harness/PROJECT_STATE.md", ".harness/CURRENT_STAGE.md"):
        if required_path not in protected:
            protected.append(required_path)
    if "codex" in cfg:
        raise HarnessError(
            "legacy [codex] configuration is not active-state compatible; "
            "migrate it explicitly to [agent_runtime] between Stages"
        )
    runtime = cfg.get("agent_runtime")
    if not isinstance(runtime, dict):
        raise HarnessError("[agent_runtime] is required")
    adapter = runtime.get("adapter_argv")
    if (not isinstance(adapter, list) or not adapter
            or not all(isinstance(item, str) and item and "\x00" not in item for item in adapter)):
        raise HarnessError("[agent_runtime].adapter_argv must be a non-empty string array")
    if runtime.get("ephemeral") is not True:
        raise HarnessError("[agent_runtime].ephemeral must be true")
    models = runtime.get("models", {})
    if not isinstance(models, dict):
        raise HarnessError("[agent_runtime.models] must be a table")
    allowed_models = {"implementer", "reviewer", "tester", "security_reviewer", "verifier"}
    if not set(models).issubset(allowed_models):
        raise HarnessError("[agent_runtime.models] contains an unknown role")
    if not all(isinstance(alias, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", alias)
               for alias in models.values()):
        raise HarnessError("[agent_runtime.models] aliases are invalid")
    if cfg.get("harness_version") == HARNESS_VERSION:
        language = cfg.get("language", {})
        required_language = {
            "internal_language": "en", "repository_language": "en",
            "agent_instruction_language": "en", "agent_report_language": "en",
            "operator_language": "auto", "correct_operator_grammar": False,
        }
        if any(language.get(key) != value for key, value in required_language.items()):
            raise HarnessError("[language] must preserve the canonical English boundary")
    return cfg


def protected_snapshot(root: pathlib.Path, cfg: dict[str, Any]) -> dict[str, Any]:
    head_result = run_capture(["git", "rev-parse", "HEAD"], cwd=root)
    head = head_result.stdout.strip()
    if head_result.returncode != 0 or not head:
        raise HarnessError("cannot bind protected snapshot to Git HEAD")
    values: dict[str, Any] = {}

    def excluded(path: pathlib.Path) -> bool:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return False
        return (
            relative == ".git" or relative.startswith(".git/")
            or relative == ".harness/state.json"
            or relative == ".harness/runtime"
            or relative.startswith(".harness/runtime/")
        )

    def reject_symlink_components(path: pathlib.Path) -> None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise HarnessError("protected path escapes the Git project") from exc
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise HarnessError(f"protected path must not contain a symlink: {relative}")

    def capture(path: pathlib.Path) -> None:
        reject_symlink_components(path)
        relative = str(path.relative_to(root))
        if not path.is_file():
            raise HarnessError(f"protected path is not a regular file: {relative}")
        if path.name == ".env" or path.name.startswith(".env.") or SECRET_NAMES.search(relative):
            info = path.stat()
            values[relative] = {
                "kind": "secret-metadata", "size": info.st_size,
                "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
                "mode": info.st_mode & 0o777, "device": info.st_dev,
                "inode": info.st_ino,
            }
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            values[relative] = {"kind": "sha256", "digest": digest}

    for pattern in cfg.get("protected_paths", []):
        if not isinstance(pattern, str):
            continue
        validate_project_relative_path(pattern, "protected path", allow_glob=True)
        if (pattern == ".git" or pattern.startswith(".git/")
                or pattern == ".harness/state.json"
                or pattern == ".harness/runtime"
                or pattern.startswith(".harness/runtime/")):
            continue
        prefix_parts: list[str] = []
        for part in pathlib.PurePath(pattern).parts:
            if any(ch in part for ch in "*?["):
                break
            prefix_parts.append(part)
        if prefix_parts:
            reject_symlink_components(root.joinpath(*prefix_parts))
        matches = list(root.glob(pattern)) if any(ch in pattern for ch in "*?[") else [root / pattern]
        for path in matches:
            if excluded(path):
                continue
            reject_symlink_components(path)
            if path.is_symlink() or path.is_file():
                capture(path)
            elif path.is_dir():
                for directory, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
                    current = pathlib.Path(directory)
                    retained: list[str] = []
                    for name in sorted(dirnames):
                        child = current / name
                        if excluded(child):
                            continue
                        reject_symlink_components(child)
                        if child.is_symlink():
                            capture(child)
                        else:
                            retained.append(name)
                    dirnames[:] = retained
                    for name in sorted(filenames):
                        child = current / name
                        if not excluded(child):
                            capture(child)
            elif path.exists():
                capture(path)
    # Bind only stable control metadata. Scanning all of .git made ordinary Git
    # operations (index locks, fetch state, reflogs, worktree registration, and
    # object maintenance) look like protected-content tampering. The resolved
    # HEAD above is the history identity; local config remains protected without
    # snapshotting Git's volatile implementation details.
    git_metadata: dict[str, Any] = {}
    for key in ("config", "config.worktree"):
        result = run_capture(["git", "rev-parse", "--git-path", key], cwd=root)
        if result.returncode != 0:
            continue
        path = pathlib.Path(result.stdout.strip())
        if not path.is_absolute():
            path = root / path
        if path.is_symlink():
            git_metadata[key] = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            git_metadata[key] = {
                "kind": "sha256", "digest": hashlib.sha256(path.read_bytes()).hexdigest()
            }
    return {"git_head": head, "git_metadata": git_metadata, "files": values}


def require_protected_unchanged(root: pathlib.Path, cfg: dict[str, Any], state: dict[str, Any]) -> None:
    baseline = state.get("protected_baseline")
    if not isinstance(baseline, dict):
        raise HarnessError("missing protected-path baseline; restart the Stage safely")
    comparable_baseline = dict(baseline)
    baseline_git_metadata = {
        key: value for key, value in dict(comparable_baseline.get("git_metadata", {})).items()
        if key in {"config", "config.worktree"}
    }
    comparable_baseline["git_metadata"] = baseline_git_metadata
    if protected_snapshot(root, cfg) != comparable_baseline:
        raise HarnessError("protected paths or Git HEAD changed during the Slice")


def _git_bytes(root: pathlib.Path, argv: list[str], error: str) -> bytes:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    cp = subprocess.run(["git", *argv], cwd=root, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, check=False, env=env)
    if cp.returncode != 0:
        raise HarnessError(error)
    return cp.stdout


def _safe_git_path(raw_path: bytes, context: str) -> pathlib.PurePosixPath:
    relative = raw_path.decode("utf-8", errors="surrogateescape")
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise HarnessError(f"invalid {context} path")
    return pure


def _hash_status(digest: Any, root: pathlib.Path, prefix: bytes = b"") -> None:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    cp = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        check=False, env=env)
    if cp.returncode != 0:
        raise HarnessError("failed to fingerprint Git worktree")
    for token in cp.stdout.split(b"\0"):
        if not token:
            continue
        raw_path = token[3:] if len(token) >= 3 and token[2:3] == b" " else token
        pure = _safe_git_path(raw_path, "Git status")
        relative = pure.as_posix()
        if relative.startswith((".git/", ".harness/")):
            continue
        digest.update(prefix)
        digest.update(token)
        path = root / relative
        digest.update(prefix)
        digest.update(raw_path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            digest.update(b"missing")
            continue
        except OSError as exc:
            raise HarnessError("failed to inspect Git worktree path") from exc
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"symlink:")
            try:
                digest.update(os.fsencode(os.readlink(path)))
            except OSError as exc:
                raise HarnessError("failed to inspect Git worktree link") from exc
        elif stat.S_ISREG(info.st_mode):
            if path.name == ".env" or path.name.startswith(".env.") or SECRET_NAMES.search(relative):
                metadata = (
                    f"secret-metadata:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}:"
                    f"{info.st_dev}:{info.st_ino}:{info.st_mode & 0o777}"
                )
                digest.update(metadata.encode())
            else:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                try:
                    fd = os.open(path, flags)
                    with os.fdopen(fd, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                except OSError as exc:
                    raise HarnessError("failed to read Git worktree path") from exc


def _hash_submodules(digest: Any, root: pathlib.Path, prefix: bytes,
                     seen: set[pathlib.Path]) -> None:
    entries = _git_bytes(root, ["ls-files", "--stage", "-z"],
                         "failed to enumerate Git submodules")
    gitlinks: list[bytes] = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_path = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise HarnessError("invalid Git index entry while fingerprinting submodules")
        if fields[0] == b"160000":
            _safe_git_path(raw_path, "submodule")
            gitlinks.append(raw_path)
    for raw_path in sorted(gitlinks):
        pure = _safe_git_path(raw_path, "submodule")
        submodule = root.joinpath(*pure.parts)
        try:
            info = submodule.lstat()
        except OSError as exc:
            raise HarnessError("Git submodule is unreadable or uninitialized") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise HarnessError("Git submodule path is not a real directory")
        resolved = submodule.resolve(strict=True)
        if resolved in seen:
            raise HarnessError("recursive Git submodule worktree detected")
        top_raw = _git_bytes(submodule, ["rev-parse", "--show-toplevel"],
                             "invalid Git submodule worktree").rstrip(b"\r\n")
        try:
            top = pathlib.Path(os.fsdecode(top_raw)).resolve(strict=True)
        except OSError as exc:
            raise HarnessError("invalid Git submodule worktree root") from exc
        if top != resolved:
            raise HarnessError("Git submodule resolves to an unexpected worktree")
        head = _git_bytes(submodule, ["rev-parse", "--verify", "HEAD^{commit}"],
                          "cannot resolve Git submodule HEAD").strip()
        if not re.fullmatch(rb"[0-9a-fA-F]{40,64}", head):
            raise HarnessError("invalid Git submodule HEAD")
        identity = prefix + raw_path
        digest.update(b"submodule\0")
        digest.update(identity)
        digest.update(b"\0")
        digest.update(head.lower())
        _hash_status(digest, submodule, identity + b"\0")
        seen.add(resolved)
        _hash_submodules(digest, submodule, identity + b"/", seen)
        seen.remove(resolved)


def worktree_fingerprint(root: pathlib.Path) -> str:
    """Bind changed content and recursive submodule state without reading secrets."""
    digest = hashlib.sha256(b"worktree-fingerprint-v3\0")
    _hash_status(digest, root)
    _hash_submodules(digest, root, b"", {root.resolve(strict=True)})
    return digest.hexdigest()


def git_head(root: pathlib.Path) -> str:
    result = run_capture(["git", "rev-parse", "HEAD"], cwd=root)
    value = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise HarnessError("cannot resolve Git HEAD for candidate identity")
    return value


def candidate_identity(root: pathlib.Path, base_sha: str | None = None) -> tuple[str, str]:
    """Return Git base and a content-bound identity for an uncommitted candidate."""
    base = base_sha or git_head(root)
    fingerprint = worktree_fingerprint(root)
    digest = hashlib.sha256(f"candidate-v1\0{base}\0{fingerprint}".encode()).hexdigest()
    return base, f"candidate-v1:{digest}"


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(q(str(v)) for v in values) + "]"


def toml_commands(commands: Iterable[Iterable[str]]) -> str:
    return "[" + ", ".join(toml_array(command) for command in commands) + "]"


def validate_project_relative_path(value: str, label: str, allow_glob: bool = False) -> str:
    path = pathlib.PurePosixPath(value)
    if (not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in value):
        raise HarnessError(f"{label} must be a normalized project-relative path: {value!r}")
    if not allow_glob and any(character in value for character in "*?["):
        raise HarnessError(f"{label} must not contain a glob: {value!r}")
    if allow_glob and any(
        any(character in part for character in "*?[") for part in path.parts[:-1]
    ):
        raise HarnessError(f"{label} glob is allowed only in the final path component: {value!r}")
    return value


def detect_project(root: pathlib.Path) -> dict[str, Any]:
    context_candidates = [
        "AGENTS.md", "CLAUDE.md", ".cursorrules", "ARCHITECTURE.md", "README.md",
        "PROJECT_BRIEF.md", "HARNESS_ADOPTION_REPORT.md",
        "README.rst", "README.txt", "docs/quality.md", "docs/exec-plans/README.md",
    ]
    context = [name for name in context_candidates if (root / name).is_file()]
    ci = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        ci = sorted(str(x.relative_to(root)) for x in workflows.iterdir()
                    if x.suffix.lower() in {".yml", ".yaml"})
    detected: list[str] = []
    workspace_members: list[str] = []
    build_entrypoints: list[str] = []
    commands: dict[str, list[str]] = {}
    project_type = "generic"
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        detected.append("Cargo.toml")
        try:
            manifest = tomllib.loads(cargo.read_text(encoding="utf-8")) if tomllib else {}
        except Exception:
            manifest = {}
        workspace = isinstance(manifest.get("workspace"), dict)
        if workspace:
            members = manifest.get("workspace", {}).get("members", [])
            if isinstance(members, list):
                workspace_members = [str(member) for member in members if isinstance(member, str)]
        project_type = "rust-workspace" if workspace else "rust"
        lock = ["--locked"] if (root / "Cargo.lock").is_file() else []
        commands = {
            "formatter": ["cargo", "fmt", "--all", "--check"],
            "lint": ["cargo", "clippy", *lock, "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings"],
            "check": ["cargo", "check", *lock, "--workspace", "--all-targets", "--all-features"],
            "unit_test": ["cargo", "test", *lock, "--workspace"],
            "integration_test": [],
        }
        for name in ("Cargo.lock", "rust-toolchain.toml", "rustfmt.toml"):
            if (root / name).is_file():
                detected.append(name)
    elif (root / "pyproject.toml").is_file():
        detected.append("pyproject.toml")
        project_type = "python"
        commands = {"formatter": ["python", "-m", "ruff", "format", "--check", "."],
                    "lint": ["python", "-m", "ruff", "check", "."],
                    "check": ["python", "-m", "compileall", "-q", "."],
                    "unit_test": ["python", "-m", "pytest", "-q"],
                    "integration_test": []}
    elif (root / "package.json").is_file():
        detected.append("package.json")
        project_type = "node"
        try:
            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
        except Exception:
            scripts = {}
        package_runner = "npm"
        commands = {
            "formatter": [package_runner, "run", "format", "--", "--check"] if "format" in scripts else [],
            "lint": [package_runner, "run", "lint"] if "lint" in scripts else [],
            "check": [package_runner, "run", "typecheck"] if "typecheck" in scripts else ([package_runner, "run", "build"] if "build" in scripts else []),
            "unit_test": [package_runner, "test", "--", "--runInBand"] if "test" in scripts else [],
            "integration_test": [package_runner, "run", "test:integration"] if "test:integration" in scripts else [],
        }
    elif (root / "go.mod").is_file():
        detected.append("go.mod")
        project_type = "go"
        commands = {"formatter": ["harness", "_gofmt-check"], "lint": ["go", "vet", "./..."],
                    "check": ["go", "test", "-run", "^$", "./..."],
                    "unit_test": ["go", "test", "./..."], "integration_test": []}
    else:
        commands = {name: [] for name in ("formatter", "lint", "check", "unit_test", "integration_test")}
    for name in ("Makefile", "Justfile", "package.json", "pyproject.toml", "go.mod"):
        if (root / name).is_file() and name not in detected:
            detected.append(name)
    if (root / "scripts" / "verify").is_file():
        build_entrypoints.append("scripts/verify")
    return {"project_type": project_type, "context": context, "ci": ci,
            "detected": detected, "workspace_members": workspace_members,
            "build_entrypoints": build_entrypoints, "commands": commands}


def render_config(root: pathlib.Path, facts: dict[str, Any]) -> str:
    commands = facts["commands"]
    is_rust = str(facts["project_type"]).startswith("rust")
    fast_gates = [["harness", "_rust-affected-check"]] if is_rust else []
    if "approved_quality_gates" in facts:
        slice_gates = [list(command) for command in facts["approved_quality_gates"]]
        stage_gates = list(slice_gates)
    elif is_rust and "scripts/verify" in facts["build_entrypoints"]:
        slice_gates = [["./scripts/verify", "fast"],
                       ["cargo", "test", "--locked", "--workspace"]]
        stage_gates = [["./scripts/verify", "full"]]
    else:
        slice_gates = [value for value in commands.values() if value]
        stage_gates = list(slice_gates)
    protected = [".git/", ".env", ".env.*", ".harness/config.toml",
                 ".harness/state.json", ".harness/PROJECT_STATE.md",
                 ".harness/CURRENT_STAGE.md", ".harness/runtime/",
                 "AGENTS.md", "ARCHITECTURE.md", "PROJECT_BRIEF.md",
                 "HARNESS_ADOPTION_REPORT.md"]
    protected.extend(path for path in facts.get("approved_protected_paths", [])
                     if path not in protected)
    return f'''config_version = {CONFIG_VERSION}
harness_version = {q(HARNESS_VERSION)}
project_name = {q(root.name)}
project_type = {q(facts["project_type"])}
context_files = {toml_array(facts["context"])}
ci_files = {toml_array(facts["ci"])}
detected_files = {toml_array(facts["detected"])}
workspace_members = {toml_array(facts["workspace_members"])}
build_entrypoints = {toml_array(facts["build_entrypoints"])}
protected_paths = {toml_array(protected)}
max_slice_attempts = 3
error_excerpt_limit = 1200
worker_timeout_seconds = 3600
gate_timeout_seconds = 1800

[commands]
formatter = {toml_array(commands["formatter"])}
lint = {toml_array(commands["lint"])}
check = {toml_array(commands["check"])}
unit_test = {toml_array(commands["unit_test"])}
integration_test = {toml_array(commands["integration_test"])}

[gates]
fast = {toml_commands(fast_gates)}
slice = {toml_commands(slice_gates)}
stage = {toml_commands(stage_gates)}

[agent_runtime]
adapter_argv = ["python3", "{{skill_root}}/adapters/codex_cli.py"]
ephemeral = true

[agent_runtime.models]

[language]
internal_language = "en"
repository_language = "en"
agent_instruction_language = "en"
agent_report_language = "en"
operator_language = "auto"
correct_operator_grammar = false

[context_maintenance]
enabled = true
idle_compaction_threshold = 0.45
max_compactions_per_worker_run = 1
min_tokens_since_last_compaction = 20000
rehydrate_after_compaction = true
'''
