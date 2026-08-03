#!/usr/bin/env python3
"""Transactional installer for the frozen Hermes + Codex Harness payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import py_compile
import runpy
import shutil
import subprocess
import sys
import tempfile
import uuid

import yaml


EXCLUDED_SUFFIXES = {".pyc"}
EXCLUDED_NAMES = {"__pycache__"}
PHASES = (
    "staging_validated", "backups_complete", "codex_skill_replaced",
    "lifecycle_skill_replaced", "plugin_replaced", "launcher_replaced",
    "project_config_replaced", "hermes_config_replaced", "post_deploy_doctor",
    "post_doctor_verified",
)


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
        "skill/SKILL.md", "skill/scripts/harness", "lifecycle-skill/SKILL.md",
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
    for relative in required[3:6]:
        json.loads((payload / relative).read_text(encoding="utf-8"))
    yaml.safe_load((payload / "plugin/plugin.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="harness-deploy-compile.") as raw:
        for index, relative in enumerate(("skill/scripts/harness", "plugin/__init__.py",
                                          "scripts/configure-hermes")):
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
        if kind == "dir":
            values[name] = {"kind": "directory", "files": tree_hashes(target)}
        else:
            values[name] = {"kind": "file", "sha256": digest(target),
                            "mode": target.stat().st_mode & 0o777}
    return values


def expected_target_manifest(targets, rendered_config: str, source_manifest: dict) -> dict:
    values = {}
    for name, _target, source, kind in targets:
        if kind == "dir":
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


def fault(phase: str) -> None:
    if os.environ.get("HARNESS_DEPLOY_FAULT_AFTER") == phase:
        raise RuntimeError(f"fault injection after deployment phase: {phase}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-root", required=True)
    parser.add_argument("--frozen-commit", required=True)
    parser.add_argument("--doctor-command-json")
    args = parser.parse_args()
    payload = Path(args.payload_root).resolve(strict=True)
    manifest = validate_payload(payload, args.frozen_commit)

    hermes = Path(os.environ.get("HERMES_DEPLOY_HOME", "/home/van-gogh/.hermes")).resolve()
    bin_dir = Path(os.environ.get("HARNESS_BIN_DIR", "/home/van-gogh/.local/bin")).resolve()
    group = Path(os.environ.get("GROUP_PROJECT_ROOT", "/home/van-gogh/project/Rust_code/Group")).resolve()
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
        ("codex_skill", hermes / "skills/software-development/codex-harness", payload / "skill", "dir"),
        ("lifecycle_skill", hermes / "skills/software-development/project-lifecycle-harness", payload / "lifecycle-skill", "dir"),
        ("plugin", hermes / "plugins/codex-harness-context", payload / "plugin", "dir"),
        ("launcher", bin_dir / "harness", payload / "bin/harness", "file"),
        ("project_config", group / ".harness/config.toml", payload / "projects/Group/config.toml", "file"),
        ("hermes_config", config_path, None, "config"),
        ("manifest", hermes / "codex-harness-deployment-manifest.json", None, "manifest"),
    ]
    expected_installed = expected_target_manifest(targets, rendered_config, manifest)
    stages = {}
    backups = {}
    existed = {}
    try:
        for name, target, source, kind in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
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
            existed[name] = target.exists() or target.is_symlink()
            if existed[name]:
                os.replace(target, backup)
            backups[name] = backup
        fault("backups_complete")

        phase_by_name = {
            "codex_skill": "codex_skill_replaced", "lifecycle_skill": "lifecycle_skill_replaced",
            "plugin": "plugin_replaced", "launcher": "launcher_replaced",
            "project_config": "project_config_replaced", "hermes_config": "hermes_config_replaced",
        }
        for name, target, _source, _kind in targets:
            os.replace(stages[name], target)
            if name in phase_by_name:
                fault(phase_by_name[name])

        for name, target, source, kind in targets:
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
        archive = hermes / "deployment-backups" / f"codex-harness-{generation}"
        archive.mkdir(parents=True, mode=0o700)
        for name, backup in backups.items():
            if not existed.get(name):
                continue
            destination = archive / name
            if backup.is_dir():
                shutil.copytree(backup, destination)
            else:
                shutil.copy2(backup, destination)
        for backup in backups.values():
            remove_exact(backup)
        print(f"deployed_commit={args.frozen_commit}")
        print(f"deployment_manifest_sha256={hashlib.sha256((hermes / 'codex-harness-deployment-manifest.json').read_bytes()).hexdigest()}")
        print(f"deployment_backup={archive}")
        return 0
    except BaseException:
        remove_exact(hermes / "deployment-backups" / f"codex-harness-{generation}")
        for name, target, _source, _kind in reversed(targets):
            if name not in existed:
                continue
            remove_exact(target)
            backup = backups.get(name)
            if existed.get(name) and backup is not None and backup.exists():
                os.replace(backup, target)
        raise
    finally:
        for stage in stages.values():
            remove_exact(stage)
        for backup in backups.values():
            remove_exact(backup)


if __name__ == "__main__":
    raise SystemExit(main())
