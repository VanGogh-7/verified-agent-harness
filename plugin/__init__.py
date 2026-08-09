"""verified-agent-harness context-maintenance tools for Hermes Agent 0.19.1.

This plugin deliberately does not mutate the active transcript from inside a
tool call. The current public PluginContext has no safe post-tool compaction API;
manual /compress and built-in automatic compaction remain authoritative.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

_SUPPORTED_HERMES = "0.19.1"


def _result(**values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _version() -> str:
    try:
        return importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _active_cli_agent(ctx):
    # PluginContext has no stable current-agent accessor in 0.19.1. This
    # read-only compatibility adapter is version-pinned and fails closed.
    if _version() != _SUPPORTED_HERMES:
        return None, None, "unsupported_hermes_version"
    manager = getattr(ctx, "_manager", None)
    cli = getattr(manager, "_cli_ref", None) if manager is not None else None
    agent = getattr(cli, "agent", None) if cli is not None else None
    if cli is None or agent is None:
        return None, None, "active_cli_session_unavailable"
    return cli, agent, None


def _project_context_config() -> dict[str, Any]:
    defaults = {"enabled": True, "idle_compaction_threshold": 0.45,
                "max_compactions_per_worker_run": 1,
                "min_tokens_since_last_compaction": 20000,
                "rehydrate_after_compaction": True}
    config = None
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        git_marker = candidate / ".git"
        if git_marker.exists():
            git_dir = git_marker
            if git_marker.is_file():
                marker = git_marker.read_text(encoding="utf-8", errors="strict").strip()
                if marker.startswith("gitdir:"):
                    git_dir = Path(marker.removeprefix("gitdir:").strip())
                    if not git_dir.is_absolute():
                        git_dir = (candidate / git_dir).resolve()
            canonical = git_dir / "harness-control" / "config.toml"
            mirror = candidate / ".harness" / "config.toml"
            if canonical.is_file() and not canonical.is_symlink():
                config = canonical
            elif mirror.is_file() and not mirror.is_symlink():
                config = mirror
            break
    if config is None:
        return defaults
    try:
        import tomllib
        with config.open("rb") as handle:
            configured = tomllib.load(handle).get("context_maintenance", {})
        values = {**defaults, **configured}
        values["enabled"] = bool(values["enabled"])
        values["idle_compaction_threshold"] = min(
            0.95, max(0.10, float(values["idle_compaction_threshold"])))
        values["max_compactions_per_worker_run"] = max(
            0, int(values["max_compactions_per_worker_run"]))
        values["min_tokens_since_last_compaction"] = max(
            0, int(values["min_tokens_since_last_compaction"]))
        values["rehydrate_after_compaction"] = bool(values["rehydrate_after_compaction"])
        return values
    except Exception:
        return defaults


def _status(ctx) -> dict[str, Any]:
    cli, agent, error = _active_cli_agent(ctx)
    if error:
        return {"supported": False, "reason": error, "hermes_version": _version(),
                "fallback": "built_in_automatic_or_manual_/compress"}
    try:
        from agent.model_metadata import estimate_request_tokens_rough
        messages = list(getattr(cli, "conversation_history", []) or [])
        system_prompt = str(getattr(agent, "_cached_system_prompt", "") or "")
        tools = getattr(agent, "tools", None) or None
        estimated = int(estimate_request_tokens_rough(
            messages, system_prompt=system_prompt, tools=tools
        ))
        compressor = getattr(agent, "context_compressor", None)
        context_length = int(getattr(compressor, "context_length", 0) or 0)
        ratio = (estimated / context_length) if context_length > 0 else None
        maintenance = _project_context_config()
        threshold = maintenance["idle_compaction_threshold"]
        return {"supported": True, "estimated_tokens": estimated,
                "context_length": context_length, "occupancy_ratio": ratio,
                "idle_compaction_threshold": threshold,
                "threshold_reached": bool(ratio is not None and ratio >= threshold),
                "maintenance_enabled": maintenance["enabled"],
                "max_compactions_per_worker_run": maintenance["max_compactions_per_worker_run"],
                "min_tokens_since_last_compaction": maintenance["min_tokens_since_last_compaction"],
                "rehydrate_after_compaction": maintenance["rehydrate_after_compaction"],
                "compression_enabled": bool(getattr(agent, "compression_enabled", False)),
                "canonical_path": "Hermes Context Engine via automatic compaction or /compress"}
    except Exception as exc:
        return {"supported": False, "reason": f"status_estimation_failed:{type(exc).__name__}",
                "fallback": "built_in_automatic_or_manual_/compress"}


def register(ctx) -> None:
    status_schema = {
        "name": "harness_context_status",
        "description": "Report current Hermes context occupancy for safe Harness idle maintenance.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    compact_schema = {
        "name": "harness_compact_if_needed",
        "description": "Evaluate Harness idle compaction; safely return the canonical fallback when in-tool compaction is unavailable.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }

    def status_handler(args, **kwargs):
        return _result(**_status(ctx))

    def compact_handler(args, **kwargs):
        value = _status(ctx)
        if not value.get("supported"):
            return _result(action="unsupported", compacted=False, **value)
        if not value.get("maintenance_enabled"):
            return _result(action="disabled_by_project", compacted=False, **value)
        if not value.get("threshold_reached"):
            return _result(action="below_threshold", compacted=False, **value)
        if value.get("compression_enabled"):
            action = "automatic_fallback"
            guidance = "Finish the current tool turn; Hermes will compact at its canonical preflight, or use /compress after the turn."
        else:
            action = "manual_required"
            guidance = "Finish the current tool turn, then use /compress. Never compress while this tool call is outstanding."
        return _result(action=action, compacted=False, guidance=guidance, **value)

    ctx.register_tool(name="harness_context_status", toolset="verified_agent_harness",
                      schema=status_schema, handler=status_handler,
                      description=status_schema["description"], emoji="🧭")
    ctx.register_tool(name="harness_compact_if_needed", toolset="verified_agent_harness",
                      schema=compact_schema, handler=compact_handler,
                      description=compact_schema["description"], emoji="🗜️")
