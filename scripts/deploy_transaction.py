#!/usr/bin/env python3
"""Transactional installer for the verified-agent-harness reference deployment."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import py_compile
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

import yaml


EXCLUDED_SUFFIXES = {".pyc"}
EXCLUDED_NAMES = {"__pycache__"}
PRE_COMMIT_PHASES = (
    "staging_validated",
    "backup_legacy_skill_before_rename", "backup_legacy_skill_after_rename",
    "backup_legacy_skill_rename_error_before_move", "backup_legacy_skill_rename_error_after_move",
    "backup_launcher_before_rename", "backup_launcher_after_rename",
    "backup_launcher_rename_error_before_move", "backup_launcher_rename_error_after_move",
    "backups_complete", "agent_skill_replaced",
    "lifecycle_skill_replaced", "plugin_replaced", "launcher_replaced",
    "project_config_replaced", "hermes_config_replaced", "post_deploy_doctor",
    "post_doctor_verified", "archive_copy_started", "archive_copy_complete",
    "archive_verified",
)


class TargetBackupState:
    def __init__(self, *, target: Path, backup: Path, existed: bool,
                 original_identity: dict | None) -> None:
        self.target = target
        self.backup = backup
        self.existed = existed
        self.original_identity = original_identity
        self.detached = False
        self.replacement_staged = False
        self.replacement_identity: dict | None = None
        self.replacement_object: tuple[int, int, int] | None = None


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def payload_manifest(payload: Path, frozen_commit: str) -> dict:
    files = {}
    for path in sorted(candidate for candidate in payload.rglob("*") if candidate.is_file()):
        relative = path.relative_to(payload)
        if any(part in EXCLUDED_NAMES for part in relative.parts) or path.suffix in EXCLUDED_SUFFIXES:
            raise RuntimeError(f"excluded generated file in payload: {relative}")
        files[str(relative)] = digest(path)
    return {"frozen_commit": frozen_commit, "files": files}


def validate_payload(payload: Path, frozen_commit: str) -> dict:
    required = (
        "skill/SKILL.md", "skill/scripts/harness", "skill/scripts/harness_core.py",
        "skill/scripts/harness_commands.py", "skill/scripts/test_harness.py",
        "skill/adapters/codex_cli.py",
        "skill/references/contracts/implementation.schema.json",
        "skill/references/contracts/review.schema.json",
        "skill/references/contracts/security-review.schema.json",
        "skill/references/contracts/test.schema.json",
        "skill/references/contracts/verification.schema.json",
        "skill/schemas/quality-gates.schema.json",
        "lifecycle-skill/SKILL.md", "lifecycle-skill/scripts/harness",
        "lifecycle-skill/schemas/analyst.schema.json",
        "lifecycle-skill/schemas/auditor.schema.json",
        "lifecycle-skill/schemas/final-review.schema.json",
        "plugin/plugin.yaml", "plugin/__init__.py", "bin/harness",
        "projects/Group/config.toml", "scripts/configure-hermes",
    )
    for relative in required:
        path = payload / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"invalid deployment payload file: {relative}")
    for path in payload.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    yaml.safe_load((payload / "plugin/plugin.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="harness-deploy-compile.") as raw:
        for index, relative in enumerate((
            "skill/scripts/harness", "skill/scripts/harness_core.py",
            "skill/scripts/harness_commands.py", "skill/scripts/test_harness.py",
            "skill/adapters/codex_cli.py", "lifecycle-skill/scripts/harness", "plugin/__init__.py",
            "scripts/configure-hermes",
        )):
            py_compile.compile(str(payload / relative), cfile=str(Path(raw) / f"{index}.pyc"), doraise=True)
    subprocess.run(["bash", "-n", str(payload / "bin/harness")], check=True)
    return payload_manifest(payload, frozen_commit)


def copy_payload(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.tmp", ".*.tmp"))
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"symlink rejected from installed payload: {path}")


def tree_hashes(root: Path) -> dict[str, dict[str, str | int]]:
    return {str(path.relative_to(root)): {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
            for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file())}


def installed_target_manifest(targets) -> dict:
    values = {}
    for name, target, _source, kind in targets:
        if kind == "remove":
            values[name] = ({"kind": "absent"} if not target.exists() and not target.is_symlink()
                            else {"kind": "unexpected", "identity": exact_object_identity(target)})
        elif kind == "dir":
            values[name] = {"kind": "directory", "files": tree_hashes(target)}
        else:
            values[name] = {"kind": "file", "sha256": digest(target),
                            "mode": target.stat().st_mode & 0o777}
    return values


def expected_target_manifest(targets, rendered_config: str, source_manifest: dict) -> dict:
    values = {}
    for name, _target, source, kind in targets:
        if kind == "remove":
            values[name] = {"kind": "absent"}
        elif kind == "dir":
            files = {str(path.relative_to(source)): {"sha256": digest(path),
                                                      "mode": path.stat().st_mode & 0o777}
                     for path in sorted(candidate for candidate in source.rglob("*") if candidate.is_file())
                     if not any(part in EXCLUDED_NAMES for part in path.relative_to(source).parts)
                     and path.suffix not in EXCLUDED_SUFFIXES}
            values[name] = {"kind": "directory", "files": files}
        else:
            content = (rendered_config.encode("utf-8") if kind == "config" else
                       (json.dumps(source_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
                       if kind == "manifest" else source.read_bytes())
            mode = (0o755 if name == "launcher" else 0o600 if name == "hermes_config" else 0o644)
            values[name] = {"kind": "file", "sha256": hashlib.sha256(content).hexdigest(),
                            "mode": mode}
    return values


def remove_exact(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def exact_object_identity(path: Path) -> dict:
    metadata = path.lstat()
    mode = metadata.st_mode & 0o777
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path), "mode": mode}
    if path.is_dir():
        return {"kind": "directory", "mode": mode,
                "entries": {child.name: exact_object_identity(child)
                            for child in sorted(path.iterdir(), key=lambda item: item.name)}}
    if path.is_file():
        return {"kind": "file", "mode": mode, "sha256": digest(path)}
    raise RuntimeError(f"unsupported deployment target object: {path}")


def object_token(path: Path) -> tuple[int, int, int]:
    """Bind a pathname to the exact root object without following links."""
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an object created by another writer."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def quarantine_owned_target(target: Path, expected_token: tuple[int, int, int],
                            quarantine: Path) -> str:
    """Move the current target aside and verify the exact object moved."""
    if not target.exists() and not target.is_symlink():
        return "absent"
    if object_token(target) != expected_token:
        return "unrecognized"
    if quarantine.exists() or quarantine.is_symlink():
        raise RuntimeError(f"rollback quarantine already exists: {quarantine}")
    os.replace(target, quarantine)
    if object_token(quarantine) != expected_token:
        return "unrecognized_quarantined"
    return "owned"


def copy_backup(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise RuntimeError(f"symlink rejected from retained deployment backup: {source}")
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def fault(phase: str) -> None:
    if os.environ.get("HARNESS_DEPLOY_FAULT_AFTER") == phase:
        raise RuntimeError(f"fault injection after deployment phase: {phase}")


def required_environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required deployment environment variable is unset: {name}")
    return Path(value).expanduser().resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-root", required=True)
    parser.add_argument("--frozen-commit", required=True)
    parser.add_argument("--doctor-command-json")
    args = parser.parse_args()
    payload = Path(args.payload_root).resolve(strict=True)
    manifest = validate_payload(payload, args.frozen_commit)

    hermes = required_environment_path("HERMES_DEPLOY_HOME")
    bin_dir = required_environment_path("HARNESS_BIN_DIR")
    group = required_environment_path("GROUP_PROJECT_ROOT")
    config_path = hermes / "config.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        raise RuntimeError("Hermes config is missing or symlinked")
    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    module = runpy.run_path(str(payload / "scripts/configure-hermes"))
    configured = module["updated_config"](raw_config)
    rendered_config = yaml.safe_dump(configured, sort_keys=False, allow_unicode=True)
    yaml.safe_load(rendered_config)

    generation = uuid.uuid4().hex
    targets = [
        ("legacy_skill", hermes / "skills/software-development/codex-harness", None, "remove"),
        ("agent_skill", hermes / "skills/software-development/verified-agent-harness", payload / "skill", "dir"),
        ("lifecycle_skill", hermes / "skills/software-development/project-lifecycle-harness", payload / "lifecycle-skill", "dir"),
        ("legacy_plugin", hermes / "plugins/codex-harness-context", None, "remove"),
        ("plugin", hermes / "plugins/verified-agent-harness-context", payload / "plugin", "dir"),
        ("launcher", bin_dir / "harness", payload / "bin/harness", "file"),
        ("project_config", group / ".harness/config.toml", payload / "projects/Group/config.toml", "file"),
        ("hermes_config", config_path, None, "config"),
        ("legacy_manifest", hermes / "codex-harness-deployment-manifest.json", None, "remove"),
        ("manifest", hermes / "verified-agent-harness-deployment-manifest.json", None, "manifest"),
    ]
    for name, target, _source, _kind in targets:
        if target.is_symlink():
            raise RuntimeError(f"deployment target is symlinked: {name}: {target}")
    expected_installed = expected_target_manifest(targets, rendered_config, manifest)
    stages = {}
    backup_states: dict[str, TargetBackupState] = {}
    committed = False
    archive = hermes / "deployment-backups" / f"verified-agent-harness-{generation}"
    try:
        for name, target, source, kind in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "remove":
                continue
            stage = target.parent / f".{target.name}.stage-{generation}"
            remove_exact(stage)
            if kind == "dir":
                copy_payload(source, stage)
            else:
                content = (rendered_config.encode("utf-8") if kind == "config" else
                           (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8") if kind == "manifest" else
                           source.read_bytes())
                stage.write_bytes(content)
                os.chmod(stage, 0o755 if name == "launcher" else 0o600 if name == "hermes_config" else 0o644)
            stages[name] = stage
        fault("staging_validated")

        for name, target, _source, _kind in targets:
            backup = target.parent / f".{target.name}.backup-{generation}"
            remove_exact(backup)
            existed = target.exists()
            state = TargetBackupState(
                target=target,
                backup=backup,
                existed=existed,
                original_identity=exact_object_identity(target) if existed else None,
            )
            backup_states[name] = state
            if not existed:
                continue
            if name in {"legacy_skill", "launcher"}:
                fault(f"backup_{name}_before_rename")
            try:
                if name in {"legacy_skill", "launcher"}:
                    fault(f"backup_{name}_rename_error_before_move")
                os.replace(target, backup)
                if name in {"legacy_skill", "launcher"}:
                    fault(f"backup_{name}_rename_error_after_move")
            except BaseException:
                # os.replace normally either succeeds or raises without mutation. Reconcile
                # the filesystem as well so an injected/platform error after the move still
                # records the only backup that rollback is allowed to trust.
                if ((backup.exists() or backup.is_symlink())
                        and not target.exists() and not target.is_symlink()
                        and exact_object_identity(backup) == state.original_identity):
                    state.detached = True
                raise
            if (not backup.exists() and not backup.is_symlink()) or target.exists() or target.is_symlink():
                raise RuntimeError(f"deployment target backup rename did not detach exactly one object: {name}")
            if exact_object_identity(backup) != state.original_identity:
                raise RuntimeError(f"deployment target backup identity mismatch: {name}")
            state.detached = True
            if name in {"legacy_skill", "launcher"}:
                fault(f"backup_{name}_after_rename")
        fault("backups_complete")

        phase_by_name = {
            "agent_skill": "agent_skill_replaced", "lifecycle_skill": "lifecycle_skill_replaced",
            "plugin": "plugin_replaced", "launcher": "launcher_replaced",
            "project_config": "project_config_replaced", "hermes_config": "hermes_config_replaced",
        }
        for name, target, _source, _kind in targets:
            state = backup_states[name]
            if _kind == "remove":
                continue
            staged_identity = exact_object_identity(stages[name])
            state.replacement_identity = staged_identity
            try:
                os.replace(stages[name], target)
            except BaseException:
                if (not stages[name].exists() and not stages[name].is_symlink()
                        and (target.exists() or target.is_symlink())
                        and exact_object_identity(target) == staged_identity):
                    state.replacement_staged = True
                    state.replacement_object = object_token(target)
                raise
            if (stages[name].exists() or stages[name].is_symlink()
                    or not target.exists() and not target.is_symlink()
                    or exact_object_identity(target) != staged_identity):
                raise RuntimeError(f"staged deployment target replacement mismatch: {name}")
            state.replacement_staged = True
            state.replacement_object = object_token(target)
            if name in phase_by_name:
                fault(phase_by_name[name])

        for name, target, source, kind in targets:
            if kind == "remove" and (target.exists() or target.is_symlink()):
                raise RuntimeError(f"legacy deployment target still exists: {name}")
            if kind == "dir" and tree_hashes(target) != tree_hashes(source):
                raise RuntimeError(f"installed tree mismatch: {name}")
            if kind == "file" and digest(target) != digest(source):
                raise RuntimeError(f"installed file mismatch: {name}")
        if yaml.safe_load(config_path.read_text(encoding="utf-8")) != configured:
            raise RuntimeError("installed Hermes configuration mismatch")
        if installed_target_manifest(targets) != expected_installed:
            raise RuntimeError("installed payload does not match frozen-source deployment manifest")

        doctor = (json.loads(args.doctor_command_json) if args.doctor_command_json else
                  [str(bin_dir / "harness"), "doctor"])
        environment = dict(os.environ)
        environment["HERMES_HOME"] = str(hermes)
        subprocess.run(doctor, cwd=group, env=environment, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        fault("post_deploy_doctor")

        actual_installed = installed_target_manifest(targets)
        if actual_installed != expected_installed:
            raise RuntimeError("installed payload changed during post-deploy doctor")
        fault("post_doctor_verified")
        archive.mkdir(parents=True, mode=0o700)
        copied = 0
        for name, state in backup_states.items():
            if not state.detached:
                continue
            if (not state.backup.exists() and not state.backup.is_symlink()
                    or exact_object_identity(state.backup) != state.original_identity):
                raise RuntimeError(f"temporary deployment backup verification failed: {name}")
            destination = archive / name
            copy_backup(state.backup, destination)
            copied += 1
            if copied == 1:
                fault("archive_copy_started")
        fault("archive_copy_complete")
        for name, state in backup_states.items():
            if state.detached and exact_object_identity(archive / name) != state.original_identity:
                raise RuntimeError(f"retained deployment backup verification failed: {name}")
        fault("archive_verified")

        # Commit point: installed targets and the complete verified retained archive
        # are now durable. Nothing below may invoke pre-commit rollback.
        committed = True
        cleanup_errors = []
        for state in backup_states.values():
            try:
                fault("temporary_backup_cleanup")
                remove_exact(state.backup)
            except BaseException as exc:
                cleanup_errors.append(f"{state.backup}: {type(exc).__name__}")
        if cleanup_errors:
            print("temporary backup cleanup incomplete; retained archive is authoritative: " +
                  "; ".join(cleanup_errors), file=sys.stderr)
        print(f"deployed_commit={args.frozen_commit}")
        print(f"deployment_manifest_sha256={hashlib.sha256((hermes / 'verified-agent-harness-deployment-manifest.json').read_bytes()).hexdigest()}")
        print(f"deployment_backup={archive}")
        return 0
    except BaseException as deployment_error:
        if committed:
            raise RuntimeError("post-commit deployment path unexpectedly raised")
        rollback_errors = []
        try:
            remove_exact(archive)
        except BaseException as exc:
            rollback_errors.append(f"archive cleanup: {type(exc).__name__}")
        try:
            archive.parent.rmdir()
        except OSError:
            pass
        for name, target, _source, _kind in reversed(targets):
            state = backup_states.get(name)
            if state is None:
                continue
            quarantine = target.with_name(f".{target.name}.rollback-{uuid.uuid4().hex}")
            quarantine_status = "absent"
            if state.replacement_staged:
                if state.replacement_object is None:
                    rollback_errors.append(f"{name}: staged replacement token unavailable")
                    continue
                try:
                    quarantine_status = quarantine_owned_target(
                        target, state.replacement_object, quarantine
                    )
                except BaseException as exc:
                    rollback_errors.append(
                        f"{name}: target quarantine failed: {type(exc).__name__}"
                    )
                    continue
                if quarantine_status == "unrecognized":
                    rollback_errors.append(
                        f"{name}: unrecognized target preserved before restore"
                    )
                    continue
                if quarantine_status == "unrecognized_quarantined":
                    rollback_errors.append(
                        f"{name}: unrelated race replacement preserved at {quarantine}"
                    )
                    continue
            elif target.exists() or target.is_symlink():
                rollback_errors.append(
                    f"{name}: unrecognized target preserved before restore"
                )
                continue

            if state.detached:
                if (not state.backup.exists() and not state.backup.is_symlink()
                        or exact_object_identity(state.backup) != state.original_identity):
                    rollback_errors.append(f"{name}: verified backup unavailable")
                    continue
                try:
                    if name == "launcher":
                        fault("restore_launcher_before_move")
                    rename_noreplace(state.backup, target)
                    if name == "launcher":
                        fault("restore_launcher_after_move")
                except BaseException as exc:
                    restored = (
                        not state.backup.exists() and not state.backup.is_symlink()
                        and (target.exists() or target.is_symlink())
                        and exact_object_identity(target) == state.original_identity
                    )
                    if not restored:
                        rollback_errors.append(
                            f"{name}: restore failed without replacement: {type(exc).__name__}"
                        )
                        continue
                if (not target.exists() and not target.is_symlink()
                        or exact_object_identity(target) != state.original_identity):
                    rollback_errors.append(f"{name}: restored identity mismatch")
                    continue
                if quarantine_status == "owned":
                    print(f"rollback_quarantine_retained={quarantine}", file=sys.stderr)
            elif state.replacement_staged and quarantine_status == "owned":
                if target.exists() or target.is_symlink():
                    rollback_errors.append(
                        f"{name}: concurrent target preserved after quarantine"
                    )
                    continue
                print(f"rollback_quarantine_retained={quarantine}", file=sys.stderr)
        if rollback_errors:
            raise RuntimeError("deployment rollback failed closed: " + "; ".join(rollback_errors)) from deployment_error
        raise
    finally:
        for stage in stages.values():
            remove_exact(stage)


if __name__ == "__main__":
    raise SystemExit(main())
