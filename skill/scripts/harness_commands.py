from __future__ import annotations

def read_approved_plan(root: pathlib.Path, raw_path: str) -> str:
    project_root = root.resolve()
    candidate = pathlib.Path(raw_path)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(project_root)
        except ValueError as exc:
            raise HarnessError("approved plan file must be inside the Git project") from exc
    else:
        relative = candidate
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise HarnessError("approved plan path must be a normalized project-relative path")
    if any(part in {".git", ".harness"} for part in relative.parts):
        raise HarnessError("approved plan file must not be inside .git or .harness")
    relative_text = str(relative)
    if relative.name == ".env" or relative.name.startswith(".env.") or SECRET_NAMES.search(relative_text):
        raise HarnessError("approved plan path is credential-shaped and will not be read")
    directory_fd = open_directory_nofollow(project_root)
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(relative.name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise HarnessError("approved plan file cannot be safely opened") from exc
    finally:
        os.close(directory_fd)
    try:
        info = validate_single_link_regular(fd, "approved plan file", require_owner=False)
        if info.st_size > 1024 * 1024:
            raise HarnessError("approved plan file exceeds 1 MiB")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise HarnessError("approved plan file exceeds 1 MiB")
        plan = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError("approved plan file is not valid UTF-8") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not plan.strip():
        raise HarnessError("the approved Stage plan file is empty")
    if any(
        character.isalpha() and ord(character) > 127
        and "LATIN" not in unicodedata.name(character, "")
        for character in plan
    ):
        raise HarnessError("the approved Stage plan must use English engineering prose")
    return plan


def initial_state() -> dict[str, Any]:
    now = utc_now()
    return {"state_version": 1, "state_revision": 0, "harness_version": HARNESS_VERSION,
            "workflow_state": "STAGE_DISCUSSION", "stage_id": None,
            "stage_title": None, "slice_id": None, "attempt": 0,
            "worker_run_seq": 0, "checkpoint_epoch": 0,
            "workflow": None, "required_assessments": [], "completed_assessments": [],
            "tester_outcome": None,
            "gates_passed": False, "gate_level": None, "base_sha": None,
            "candidate_id": None, "validated_worktree": None,
            "verifier_state": "not_required", "evidence_counters": {},
            "worker": None, "owner": None,
            "created_at": now, "updated_at": now,
            "recent_transitions": []}


def require_v1_active_state(state: dict[str, Any]) -> None:
    if state.get("harness_version") != HARNESS_VERSION:
        raise HarnessError(
            "active pre-1.0 Stage cannot be migrated in place; migrate only between Stages"
        )


def init_project(_args: argparse.Namespace) -> None:
    root = git_root(True)
    p = paths(root)
    p["runtime"].mkdir(parents=True, exist_ok=True)
    facts = detect_project(root)
    created: list[str] = []
    files = {
        p["config"]: render_config(root, facts),
        p["state"]: json.dumps(initial_state(), indent=2, sort_keys=True) + "\n",
        p["project"]: f"# Project State\n\nProject: {root.name}\n\nInitialized: {utc_now()}\n\n## Current status\n\nHarness initialized; no Stage approved.\n",
        p["stage"]: "# Current Stage\n\nNo Stage has been approved. Discuss direction, architecture, Stage, and Slices with the user first.\n",
    }
    for path, content in files.items():
        if not path.exists():
            atomic_write(path, content)
            created.append(str(path.relative_to(root)))
    p["trusted"].mkdir(parents=True, exist_ok=True, mode=0o700)
    if not p["trusted_config"].exists():
        atomic_write(p["trusted_config"], read_single_link_text(p["config"], "project config"), mode=0o600)
    if not p["trusted_state"].exists():
        atomic_write(p["trusted_state"], read_single_link_text(p["state"], "project state"), mode=0o600)
    if not p["trusted_stage"].exists():
        atomic_write(p["trusted_stage"], read_single_link_text(p["stage"], "current Stage"), mode=0o600)
    ignore = root / ".gitignore"
    old = read_single_link_text(ignore, ".gitignore", require_owner=False) if ignore.exists() else ""
    entry = "/.harness/runtime/"
    if entry not in {line.strip() for line in old.splitlines()}:
        suffix = "" if not old or old.endswith("\n") else "\n"
        atomic_write(ignore, old + suffix + entry + "\n")
        created.append(".gitignore")
    emit("init", "ok", project_type=facts["project_type"], created=created)


def executable_version(name: str, args: list[str]) -> tuple[bool, str]:
    path = shutil.which(name)
    if not path:
        return False, "missing"
    try:
        cp = run_capture([path, *args], timeout=20)
        return cp.returncode == 0, concise(cp.stdout, 240)
    except Exception as exc:
        return False, concise(str(exc), 240)


def doctor(args: argparse.Namespace) -> None:
    root = git_root(False)
    checks: dict[str, Any] = {}
    for name, argv in (("git", ["--version"]), ("python", ["--version"])):
        executable = sys.executable if name == "python" else name
        checks[name] = executable_version(executable, argv)
    cfg = None
    if root and paths(root)["config"].exists():
        try:
            cfg = load_config(paths(root))
            checks["project_config"] = cfg.get("harness_version") in COMPATIBLE_HARNESS_VERSIONS
        except Exception:
            checks["project_config"] = False
            checks["agent_adapter"] = False
    if cfg is not None:
        try:
            adapter = expanded_adapter_argv(cfg)
            result = run_capture([*adapter, "--describe"], timeout=20)
            description = json.loads(result.stdout) if result.returncode == 0 else None
            checks["agent_adapter"] = adapter_contract_compatible(description)
        except Exception:
            checks["agent_adapter"] = False
    if args.capabilities:
        try:
            enable_child_subreaper()
            checks["linux_child_subreaper"] = True
        except HarnessError:
            checks["linux_child_subreaper"] = False
        probe_cgroup: pathlib.Path | None = None
        try:
            probe_cgroup = create_worker_cgroup()
            destroy_worker_cgroup(probe_cgroup, kill=True)
            probe_cgroup = None
            checks["cgroup_v2_optional"] = True
        except HarnessError:
            checks["cgroup_v2_optional"] = "unavailable (trusted-local process-group fallback active)"
        finally:
            if probe_cgroup is not None:
                with contextlib.suppress(HarnessError):
                    destroy_worker_cgroup(probe_cgroup, kill=True)
    required = {"git", "python"}
    if "project_config" in checks:
        required.update({"project_config", "agent_adapter"})
    if args.capabilities:
        required.add("linux_child_subreaper")
    ok = all(
        checks[name][0] if isinstance(checks[name], tuple) else checks[name] is not False
        for name in required
    )
    emit("doctor", "ok" if ok else "degraded", checks=checks)
    if not ok:
        raise HarnessError("one or more doctor checks failed or require compatibility review")


def status(args: argparse.Namespace) -> None:
    root = git_root(True)
    p = paths(root)
    state = load_state(p)
    checkpoint_path = p["trusted_checkpoint"] if p["trusted_checkpoint"].exists() else p["checkpoint"]
    checkpoint = load_json(checkpoint_path) if checkpoint_path.exists() else None
    value = {"project": root.name, "workflow_state": state["workflow_state"],
             "stage_id": state.get("stage_id"), "slice_id": state.get("slice_id"),
             "attempt": state.get("attempt"),
             "worker_run_seq": state.get("worker_run_seq", 0),
             "checkpoint_epoch": state.get("checkpoint_epoch", 0),
             "workflow": state.get("workflow"), "base_sha": state.get("base_sha"),
             "candidate_id": state.get("candidate_id"), "gates_passed": state.get("gates_passed"),
             "required_assessments": state.get("required_assessments", []),
             "completed_assessments": state.get("completed_assessments", []),
             "verifier_state": state.get("verifier_state", "not_required"),
             "evidence_counters": state.get("evidence_counters", {}), "worker": state.get("worker"),
             "checkpoint": checkpoint}
    if args.json:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        emit("status", "ok", **value)


def bind_session(args: argparse.Namespace) -> None:
    """Bind an orchestrator process session to the active Harness generation."""
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", args.session_id):
        raise HarnessError("session ID contains unsupported characters")
    root = git_root(True)
    p = paths(root)
    with state_lock(p):
        state = load_state(p)
        require_v1_active_state(state)
        if state["workflow_state"] not in {"IMPLEMENTING", "ASSESSING", "VERIFYING"}:
            raise HarnessError("bind-session requires an active worker")
        owner = state.get("owner") or {}
        worker = state.get("worker") or {}
        if (owner.get("generation") != args.generation
                or worker.get("generation") != args.generation):
            raise HarnessError("session binding generation does not match the active worker")
        checkpoint = load_json(p["trusted_checkpoint"])
        if checkpoint.get("generation") != args.generation:
            raise HarnessError("canonical checkpoint generation mismatch")
        existing = checkpoint.get("worker_session_id")
        if existing not in {None, args.session_id}:
            raise HarnessError("active generation is already bound to a different session")
        worker["session_id"] = args.session_id
        state["worker"] = worker
        save_state(p, state, owner_generation=args.generation)
        checkpoint["worker_session_id"] = args.session_id
        checkpoint["updated_at"] = utc_now()
        atomic_json(p["trusted_checkpoint"], checkpoint)
        atomic_runtime_json(p["checkpoint"], checkpoint)
    emit("bind-session", "ok", generation=args.generation, session_id=args.session_id)


def sync_config(_args: argparse.Namespace) -> None:
    """Explicitly import project config while no Stage is active."""
    root = git_root(True)
    p = paths(root)
    with state_lock(p):
        state = load_state(p)
        if state["workflow_state"] not in {"STAGE_DISCUSSION", "STAGE_COMPLETED"}:
            raise HarnessError("sync-config is allowed only between active Stages")
        raw = p["config"].read_bytes()
        if tomllib is None:
            raise HarnessError("Python 3.11+ is required (tomllib unavailable)")
        try:
            cfg = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise HarnessError("project Harness config is invalid") from exc
        if cfg.get("config_version") != CONFIG_VERSION or cfg.get("harness_version") != HARNESS_VERSION:
            raise HarnessError("project Harness config version does not match this Harness")
        atomic_write(p["trusted_config"], raw.decode("utf-8"), mode=0o600)
        state["harness_version"] = HARNESS_VERSION
        state.setdefault("state_revision", 0)
        state.setdefault("owner", None)
        save_state(p, state)
    emit("sync-config", "ok", harness_version=HARNESS_VERSION)


def project_current_status(state: dict[str, Any]) -> str:
    stage = state.get("stage_id")
    title = state.get("stage_title")
    slice_id = state.get("slice_id")
    if state.get("workflow_state") == "STAGE_COMPLETED":
        return f"No active Stage. Most recently completed: {stage} — {title}."
    if stage and title and slice_id:
        return f"Active Stage {stage} / Slice {slice_id}: {title}."
    return "Harness initialized; no Stage approved."


def write_project_current_status(p: dict[str, pathlib.Path], state: dict[str, Any]) -> None:
    project = read_single_link_text(p["project"], "project state")
    pattern = re.compile(r"(?ms)(^## Current status\n\n).*?(?=^## |\Z)")
    updated, count = pattern.subn(
        lambda match: match.group(1) + project_current_status(state) + "\n\n",
        project,
        count=1,
    )
    if count != 1:
        raise HarnessError("project state is missing exactly one Current status section")
    atomic_write(p["project"], updated.rstrip() + "\n")


def sync_project_state(_args: argparse.Namespace) -> None:
    """Synchronize protected project-status prose from canonical Harness state."""
    root = git_root(True)
    p = paths(root)
    cfg = load_config(p)
    with state_lock(p):
        state = load_state(p)
        require_protected_unchanged(root, cfg, state)
        write_project_current_status(p, state)
        state["protected_baseline"] = protected_snapshot(root, cfg)
        save_state(p, state)
    emit("sync-project-state", "ok", state=state["workflow_state"], stage=state.get("stage_id"))


def start_stage(args: argparse.Namespace) -> None:
    root = git_root(True)
    if not re.search(r"[A-Za-z]", args.title):
        raise HarnessError("Stage title must be English")
    p = paths(root)
    plan = read_approved_plan(root, args.plan_file)
    if len(re.findall(r"[A-Za-z]", plan)) < 20:
        raise HarnessError("the approved Stage plan must be an English engineering artifact")
    with state_lock(p):
        state = load_state(p)
        if state["workflow_state"] not in {"STAGE_DISCUSSION", "STAGE_COMPLETED"}:
            raise HarnessError("start-stage requires STAGE_DISCUSSION or STAGE_COMPLETED")
        atomic_write(p["trusted_config"], p["config"].read_text(encoding="utf-8"), mode=0o600)
        cfg = load_config(p)
        if cfg.get("harness_version") != HARNESS_VERSION:
            raise HarnessError("migrate and review project config before starting a 1.0 Stage")
        body = f"# Current Stage\n\n- Stage: {args.stage}\n- Title: {args.title}\n- Current Slice: {args.slice}\n- Approved: {utc_now()}\n\n## Plan and acceptance criteria\n\n{plan.rstrip()}\n"
        atomic_write(p["stage"], body)
        atomic_write(p["trusted_stage"], body, mode=0o600)
        clear_trusted_evidence(p)
        required = ["reviewer", "tester"]
        if args.workflow == "SECURITY":
            required.append("security_reviewer")
        state.update({"stage_id": args.stage, "stage_title": args.title,
                      "slice_id": args.slice, "attempt": 0,
                      "harness_version": HARNESS_VERSION,
                      "workflow": args.workflow, "required_assessments": required,
                      "completed_assessments": [], "gates_passed": False,
                      "gate_level": None, "base_sha": None, "candidate_id": None,
                      "verifier_state": "pending", "evidence_counters": {}, "worker": None})
        record_transition(state, "STAGE_APPROVED", "user-approved Stage recorded")
        record_transition(state, "SLICE_READY", "first approved Slice ready")
        write_project_current_status(p, state)
        state["protected_baseline"] = protected_snapshot(root, cfg)
        save_state(p, state)
    emit("start-stage", "ok", state="SLICE_READY", stage=args.stage, slice=args.slice,
         workflow=args.workflow, max_attempts=cfg["max_slice_attempts"])


def skill_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def schema_path(role: str) -> pathlib.Path:
    """Return the mutable agent handoff contract from an allowed Skill path."""
    names = {"implementer": "implementation.schema.json", "reviewer": "review.schema.json",
             "tester": "test.schema.json", "security_reviewer": "security-review.schema.json",
             "verifier": "verification.schema.json"}
    if role not in names:
        raise HarnessError(f"unknown agent role: {role}")
    name = names[role]
    return skill_root() / "references" / "contracts" / name


def advisory_schema_path() -> pathlib.Path:
    """Return the single non-authoritative advisory handoff contract."""
    return skill_root() / "references" / "contracts" / "advisory.schema.json"


def validate_schema_value(value: Any, schema: dict[str, Any], where: str = "$") -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise HarnessError(f"{where} must be an object")
        for key in schema.get("required", []):
            if key not in value:
                raise HarnessError(f"{where}.{key} is required")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(schema.get("properties", {}))
            if extras:
                raise HarnessError(f"{where} has unexpected fields: {sorted(extras)}")
        for key, item_schema in schema.get("properties", {}).items():
            if key in value:
                validate_schema_value(value[key], item_schema, f"{where}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise HarnessError(f"{where} must be an array")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise HarnessError(f"{where} must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise HarnessError(f"{where} must contain at most {schema['maxItems']} items")
        for idx, item in enumerate(value):
            validate_schema_value(item, schema.get("items", {}), f"{where}[{idx}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise HarnessError(f"{where} must be a string")
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise HarnessError(f"{where} is shorter than minLength={schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise HarnessError(f"{where} exceeds maxLength={schema['maxLength']}")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise HarnessError(f"{where} must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise HarnessError(f"{where} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise HarnessError(f"{where} must be <= {schema['maximum']}")
    elif expected == "boolean" and not isinstance(value, bool):
        raise HarnessError(f"{where} must be a boolean")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError(f"{where} must be one of {schema['enum']}")


def validate_report(path: pathlib.Path, schema_file: pathlib.Path) -> dict[str, Any]:
    report = load_json(path)
    schema = load_json(schema_file)
    validate_schema_value(report, schema)
    if schema_file.name == "implementation.schema.json":
        if report.get("status") == "completed" and report.get("blockers"):
            raise HarnessError("a completed implementation must contain no blockers")
        if report.get("status") == "blocked" and not report.get("blockers"):
            raise HarnessError("a blocked implementation must describe at least one blocker")
    if schema_file.name in {"review.schema.json", "security-review.schema.json"}:
        if report.get("status") == "blocked" and not report.get("limitations"):
            raise HarnessError("a blocked assessor must describe a limitation")
    if schema_file.name == "test.schema.json":
        if report.get("status") == "blocked" and not report.get("limitations"):
            raise HarnessError("a blocked Tester report must describe a limitation")
        if report.get("outcome") == "failed" and not report.get("findings"):
            raise HarnessError("a failed Tester report must contain a finding")
        if report.get("outcome") == "failed" and not any(
            finding.get("blocking_recommendation")
            and finding.get("severity") in {"critical", "high"}
            for finding in report.get("findings", [])
        ):
            raise HarnessError("a failed Tester report must contain a policy-blocking hypothesis")
        if report.get("outcome") == "flaky_or_infra" and not report.get("limitations"):
            raise HarnessError("flaky_or_infra Tester evidence must describe a limitation")
    if schema_file.name == "verification.schema.json":
        classifications = report.get("classifications", [])
        confirmed = any(item.get("classification") == "confirmed" and item.get("policy_blocking")
                        for item in classifications)
        inconclusive_high = any(item.get("classification") == "inconclusive" and item.get("policy_blocking")
                                for item in classifications)
        flaky_high = any(item.get("classification") == "flaky_or_infra" and item.get("policy_blocking")
                         for item in classifications)
        if report.get("decision") == "changes_required" and not confirmed:
            raise HarnessError("changes_required requires a confirmed policy-blocking finding")
        if confirmed and report.get("decision") != "changes_required":
            raise HarnessError("confirmed policy-blocking findings require changes_required")
        if inconclusive_high and report.get("decision") != "blocked":
            raise HarnessError("policy-blocking inconclusive findings require a human-blocked decision")
        if flaky_high and report.get("decision") != "blocked":
            raise HarnessError("policy-blocking flaky_or_infra findings require a blocked decision")
        if report.get("status") == "blocked" and report.get("decision") != "blocked":
            raise HarnessError("a blocked Verifier report must use decision=blocked")
    if schema_file.name == "quality-gates.schema.json":
        gates = report.get("gates", [])
        executed = [gate for gate in gates if gate.get("status") != "skipped"]
        if not executed:
            raise HarnessError("quality-gate report must contain at least one executed gate")
        passed = all(gate.get("status") == "passed" for gate in executed)
        if (report.get("status") == "passed") != passed:
            raise HarnessError("quality-gate status is inconsistent with individual gate results")
        failure_class = report.get("failure_class")
        if failure_class is None:
            # Backward-compatible and fail-closed: legacy failed reports can
            # never acquire the narrow environment-retry privilege.
            failure_class = "none" if passed else "product"
            report["failure_class"] = failure_class
        if passed and failure_class != "none":
            raise HarnessError("a passing quality-gate report must use failure_class=none")
        if not passed and failure_class != "product":
            raise HarnessError("a failed quality-gate report must classify the failure")
        for gate in executed:
            if not gate.get("command") or not gate.get("resolved_command"):
                raise HarnessError("every executed gate must record non-empty commands")
            exit_code = gate.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                raise HarnessError("every executed gate must record an integer exit_code")
            if (gate.get("status") == "passed") != (exit_code == 0):
                raise HarnessError("gate status is inconsistent with exit_code")
    return report


def require_candidate_binding(root: pathlib.Path, state: dict[str, Any], report: dict[str, Any],
                              role: str) -> None:
    expected_base, expected_candidate = candidate_identity(root, state.get("base_sha"))
    if report.get("base_sha") != expected_base or report.get("candidate_id") != expected_candidate:
        raise HarnessError(f"{role} report candidate identity does not match the current worktree")
    if role != "gates" and report.get("attempt") != state.get("attempt"):
        raise HarnessError(f"{role} report attempt does not match current state")
    if role == "verifier":
        if state.get("tester_outcome") == "flaky_or_infra":
            raise HarnessError("Verifier cannot proceed after flaky_or_infra required testing")
        source: dict[str, dict[str, Any]] = {}
        for findings in state.get("assessor_findings", {}).values():
            for finding in findings:
                finding_id = finding.get("id")
                if finding_id in source:
                    raise HarnessError("assessor finding IDs must be globally unique")
                source[finding_id] = finding
        classifications = report.get("classifications", [])
        classified = [item.get("finding_id") for item in classifications]
        if len(classified) != len(set(classified)) or set(classified) != set(source):
            raise HarnessError("Verifier must classify every assessor finding exactly once")
        confirmed_blocking = False
        unresolved_blocking = False
        for item in classifications:
            finding = source[item["finding_id"]]
            expected_blocking = bool(
                finding.get("blocking_recommendation")
                and finding.get("severity") in {"critical", "high"}
            )
            if item.get("policy_blocking") != expected_blocking:
                raise HarnessError("Verifier policy_blocking does not match finding policy")
            confirmed_blocking |= expected_blocking and item["classification"] == "confirmed"
            unresolved_blocking |= expected_blocking and item["classification"] in {
                "inconclusive", "flaky_or_infra",
            }
        expected_decision = ("changes_required" if confirmed_blocking else
                             "blocked" if unresolved_blocking else "approved")
        if report.get("decision") != expected_decision:
            raise HarnessError("Verifier decision is inconsistent with verified finding policy")


def render_prompt(root: pathlib.Path, p: dict[str, pathlib.Path], cfg: dict[str, Any],
                  state: dict[str, Any], role: str) -> str:
    template_name = {"implementer": "implementation-prompt.md", "reviewer": "review-prompt.md",
                     "tester": "test-prompt.md", "security_reviewer": "security-review-prompt.md",
                     "verifier": "verification-prompt.md"}[role]
    template = (skill_root() / "templates" / template_name).read_text(encoding="utf-8")
    stage_source = p["trusted_stage"] if p["trusted_stage"].exists() else p["stage"]
    stage_text = read_single_link_text(stage_source, "current Stage")
    context_files = "\n".join(f"- {x}" for x in cfg.get("context_files", [])) or "- none"
    protected = "\n".join(f"- {x}" for x in cfg.get("protected_paths", [])) or "- none"
    replacements = {"{{PROJECT_ROOT}}": str(root), "{{STAGE_ID}}": str(state.get("stage_id")),
                    "{{SLICE_ID}}": str(state.get("slice_id")), "{{ATTEMPT}}": str(state.get("attempt")),
                    "{{BASE_SHA}}": str(state.get("base_sha") or git_head(root)),
                    "{{CANDIDATE_ID}}": str(state.get("candidate_id") or "run harness candidate-id after editing"),
                    "{{CONTEXT_FILES}}": context_files, "{{PROTECTED_PATHS}}": protected,
                    "{{REPAIR_CONTEXT}}": json.dumps(state.get("repair_context", []),
                                                       ensure_ascii=True, indent=2),
                    "{{CURRENT_STAGE}}": stage_text}
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def render_advisory_prompt(root: pathlib.Path, p: dict[str, pathlib.Path], state: dict[str, Any],
                           role: str, base_sha: str, candidate_id: str) -> str:
    if role not in ADVISORY_ROLE_KEYS:
        raise HarnessError(f"unknown advisory role: {role}")
    template = (skill_root() / "templates" / "advisory-prompt.md").read_text(encoding="utf-8")
    stage_source = p["trusted_stage"] if p["trusted_stage"].exists() else p["stage"]
    replacements = {
        "{{ADVISORY_ROLE}}": ROLE_LABELS[role],
        "{{PROJECT_ROOT}}": str(root),
        "{{STAGE_ID}}": str(state.get("stage_id")),
        "{{SLICE_ID}}": str(state.get("slice_id")),
        "{{ATTEMPT}}": str(state.get("attempt")),
        "{{BASE_SHA}}": base_sha,
        "{{CANDIDATE_ID}}": candidate_id,
        "{{CURRENT_STAGE}}": read_single_link_text(stage_source, "current Stage"),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def process_identity(pid: Any) -> dict[str, Any] | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        stat_text = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = stat_text[stat_text.rfind(")") + 2:].split()
        start_time = remainder[19]
        executable = os.readlink(f"/proc/{pid}/exe")
        return {"start_time": start_time, "executable": executable, "session_id": os.getsid(pid)}
    except OSError:
        return None


def process_alive(pid: Any, expected: Any) -> bool:
    return isinstance(expected, dict) and process_identity(pid) == expected


def owner_alive(owner: Any) -> bool:
    return isinstance(owner, dict) and process_alive(owner.get("pid"), owner.get("identity"))


def assert_generation(state: dict[str, Any], generation: str, role: str | None = None) -> None:
    owner = state.get("owner") or {}
    worker = state.get("worker") or {}
    if owner.get("generation") != generation or worker.get("generation") != generation:
        raise HarnessError("stale Harness generation cannot update this run")
    if role is not None and worker.get("role") != role:
        raise HarnessError("stale worker role cannot update this run")


ROLE_LABELS = {
    "implementer": "Implementer",
    "reviewer": "Correctness Reviewer",
    "tester": "Tester",
    "security_reviewer": "Security Reviewer",
    "verifier": "Verifier",
    "explorer": "Explorer",
    "researcher": "Researcher",
    "test_triage": "Test Triage",
    "log_triage": "Log Triage",
    "architecture_analyst": "Architecture Analyst",
    "auditor": "Independent Auditor",
    "final_lifecycle_reviewer": "Final Lifecycle Reviewer",
}


def expanded_adapter_argv(cfg: dict[str, Any]) -> list[str]:
    configured = cfg.get("agent_runtime", {}).get("adapter_argv")
    if (not isinstance(configured, list) or not configured
            or not all(isinstance(item, str) and item and "\x00" not in item for item in configured)):
        raise HarnessError("[agent_runtime].adapter_argv must be a non-empty string array")
    root = str(skill_root())
    return [item.replace("{skill_root}", root) for item in configured]


def agent_adapter_argv(cfg: dict[str, Any], root: pathlib.Path, role: str,
                       prompt: pathlib.Path, schema: pathlib.Path,
                       output: pathlib.Path) -> list[str]:
    if role not in ROLE_LABELS:
        raise HarnessError(f"unknown agent role: {role}")
    runtime = cfg.get("agent_runtime", {})
    if runtime.get("ephemeral") is not True:
        raise HarnessError("[agent_runtime].ephemeral must be true")
    models, reasoning_efforts = runtime_role_routing(runtime)
    model = models.get(role, "")
    reasoning_effort = reasoning_efforts.get(role, "")
    access = "workspace-write" if role == "implementer" else "read-only"
    return [
        *expanded_adapter_argv(cfg),
        "--role", ROLE_LABELS[role],
        "--access", access,
        "--workdir", str(root),
        "--prompt", str(prompt),
        "--schema", str(schema),
        "--output", str(output),
        "--model-alias", model,
        "--reasoning-effort", reasoning_effort,
        "--ephemeral", "true",
    ]


def worker_environment() -> dict[str, str]:
    """Pass only the bounded process context needed by a configured runtime."""
    allowed = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR",
               "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
               "CARGO_BUILD_JOBS"}
    result = {key: value for key, value in os.environ.items()
              if key in allowed or key.startswith("LC_")}
    result.update({"GIT_OPTIONAL_LOCKS": "0", "CONDA_NO_PLUGINS": "true"})
    result.setdefault("CARGO_BUILD_JOBS", "4")
    return result


def gate_environment(temporary_home: pathlib.Path) -> dict[str, str]:
    """Build a proxy-stripped allowlist; trusted toolchain homes may contain credentials."""
    allowed = {
        "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "CARGO_HOME", "RUSTUP_HOME",
        "CARGO_TARGET_DIR", "RUSTFLAGS", "CARGO_NET_OFFLINE", "GROUP_VERIFY_OFFLINE",
        "CARGO_BUILD_JOBS",
    }
    result = {key: value for key, value in os.environ.items()
              if key in allowed or key.startswith("LC_")}
    result.update({"HOME": str(temporary_home), "XDG_CONFIG_HOME": str(temporary_home / ".config"),
                   "XDG_CACHE_HOME": str(temporary_home / ".cache"),
                   "XDG_DATA_HOME": str(temporary_home / ".local" / "share"),
                   "GIT_OPTIONAL_LOCKS": "0", "CONDA_NO_PLUGINS": "true",
                   "PYTHONNOUSERSITE": "1"})
    result.setdefault("CARGO_BUILD_JOBS", "4")
    return result


def write_checkpoint(p: dict[str, pathlib.Path], state: dict[str, Any], role: str,
                     status_value: str, pid: int | None, report: pathlib.Path,
                     wake_action: str, generation: str, mirror: bool = True) -> None:
    assert_generation(state, generation, role)
    cfg = load_config(p)
    cm = cfg.get("context_maintenance", {})
    worker_session_id = None
    if p["trusted_checkpoint"].exists():
        with contextlib.suppress(HarnessError):
            previous = load_json(p["trusted_checkpoint"])
            if previous.get("generation") == generation:
                worker_session_id = previous.get("worker_session_id")
    value = {"stage_id": state.get("stage_id"), "slice_id": state.get("slice_id"),
             "workflow_state": state.get("workflow_state"), "worker_role": role,
             "worker_status": status_value, "worker_session_id": worker_session_id,
             "process_id": pid, "process_identity": process_identity(pid),
             "owner_pid": (state.get("owner") or {}).get("pid"),
             "owner_identity": (state.get("owner") or {}).get("identity"),
             "generation": generation,
             "worker_cgroup": (state.get("worker") or {}).get("cgroup"),
             "preflight": (state.get("worker") or {}).get("preflight"),
             "attempt": state.get("attempt"),
             "base_sha": state.get("base_sha"), "candidate_id": state.get("candidate_id"),
             "worker_run_seq": state.get("worker_run_seq", 0),
             "checkpoint_epoch": state.get("checkpoint_epoch", 0),
             "expected_output_file": str(report), "next_action_after_completion": wake_action,
             "unresolved_findings": state.get("unresolved_findings", []),
             "important_user_constraints": ["Do not launch unapproved work", "Do not expose worker logs or secrets"],
             "compactions_this_worker_run": 0,
             "max_compactions_per_worker_run": cm.get("max_compactions_per_worker_run", 1),
             "updated_at": utc_now()}
    atomic_json(p["trusted_checkpoint"], value)
    if mirror:
        atomic_runtime_json(p["checkpoint"], value)


RELEASABLE_IMPLEMENTER_FAILURES = {
    "spawn_exec_failure",
    "agent_output_schema_startup_rejection",
}


def release_infrastructure_attempt(state: dict[str, Any], role: str | None,
                                   worker: dict[str, Any], failure_class: str | None,
                                   root: pathlib.Path) -> None:
    """Release only a recognized pre-business failure on an unchanged worktree."""
    worker["business_attempt_committed"] = True
    if (role != "implementer" or failure_class not in RELEASABLE_IMPLEMENTER_FAILURES
            or worker.get("launch_worktree_fingerprint") != worktree_fingerprint(root)):
        return
    candidate = worker.get("attempt")
    current = int(state.get("attempt", 0))
    if isinstance(candidate, int) and candidate == current and current > 0:
        state["attempt"] = current - 1
        worker["attempt_candidate"] = candidate
        worker["attempt"] = state["attempt"]
        worker["business_attempt_committed"] = False


def invalidate_checkpoint(p: dict[str, pathlib.Path]) -> None:
    """Remove a checkpoint that could not be synchronized with canonical state."""
    with contextlib.suppress(FileNotFoundError):
        p["trusted_checkpoint"].unlink()
    with contextlib.suppress(HarnessError):
        runtime_unlink(p["checkpoint"])


def persist_worker_handoff(p: dict[str, pathlib.Path], state: dict[str, Any],
                           role: str, result: dict[str, Any], generation: str,
                           report: pathlib.Path, pid: int | None) -> None:
    """Persist an accepted handoff and a checkpoint derived from the same state."""
    assert_generation(state, generation, role)
    state["worker"] = {
        "role": role, "status": "completed", "completed_at": utc_now(),
        "expected_output": str(report), "generation": generation,
        "stage_id": state.get("stage_id"), "slice_id": state.get("slice_id"),
        "attempt": state.get("attempt"),
        "base_sha": state.get("base_sha"), "candidate_id": state.get("candidate_id"),
    }
    if role == "implementer":
        if result["status"] == "completed":
            if state["workflow_state"] == "IMPLEMENTING":
                record_transition(state, "VALIDATING", "Implementer handoff accepted")
            elif state["workflow_state"] != "VALIDATING":
                raise HarnessError(
                    f"Implementer completed after incompatible state {state['workflow_state']}"
                )
        elif state["workflow_state"] == "IMPLEMENTING":
            record_transition(state, "BLOCKED", "Implementer reported blocked")
    elif role in {"reviewer", "tester", "security_reviewer"}:
        if role == "tester" and result.get("outcome") == "flaky_or_infra":
            state["tester_outcome"] = "flaky_or_infra"
            findings = state.setdefault("assessor_findings", {})
            findings[role] = result.get("findings", [])
            counters = state.setdefault("evidence_counters", {})
            counters["tester_findings"] = len(result.get("findings", []))
            record_transition(
                state, "BLOCKED",
                "required testing was flaky_or_infra; infrastructure or human action required",
            )
        elif result["status"] == "blocked":
            record_transition(state, "BLOCKED", f"{role} reported an evidence or tooling blocker")
        else:
            if role == "tester":
                state["tester_outcome"] = result.get("outcome")
            completed = state.setdefault("completed_assessments", [])
            if role not in completed:
                completed.append(role)
            findings = state.setdefault("assessor_findings", {})
            findings[role] = result.get("findings", [])
            state["unresolved_findings"] = [
                finding for values in findings.values() for finding in values
            ]
            counters = state.setdefault("evidence_counters", {})
            counters[f"{role}_findings"] = len(result.get("findings", []))
    else:
        state["verifier_state"] = result["decision"]
        counters = state.setdefault("evidence_counters", {})
        for classification in ("confirmed", "rejected", "inconclusive", "flaky_or_infra"):
            counters[classification] = sum(
                item.get("classification") == classification for item in result.get("classifications", [])
            )
        classifications = {item["finding_id"]: item for item in result.get("classifications", [])}
        source_findings = [finding for values in state.get("assessor_findings", {}).values()
                           for finding in values]
        state["repair_context"] = [
            finding for finding in source_findings
            if classifications.get(finding.get("id"), {}).get("classification")
            in {"confirmed", "inconclusive"}
            and classifications.get(finding.get("id"), {}).get("policy_blocking")
        ]
        if result["decision"] == "approved":
            state["repair_context"] = []
            record_transition(state, "APPROVED", "Verifier approved the complete evidence join")
        elif result["decision"] == "changes_required":
            record_transition(state, "CHANGES_REQUIRED", "Verifier confirmed policy-blocking findings")
        else:
            record_transition(state, "BLOCKED", "Verifier could not reach a safe decision")
    save_state(p, state, owner_generation=generation)
    write_checkpoint(
        p, state, role, "completed", pid, report,
        "run-gates" if role == "implementer" else
        "run-verifier" if role in {"reviewer", "tester", "security_reviewer"} else "approve-slice",
        generation,
    )


def persist_blocked_worker(p: dict[str, pathlib.Path], state: dict[str, Any],
                           role: str, worker: dict[str, Any], reason: str,
                           generation: str, report: pathlib.Path,
                           pid: int | None, *, failure_class: str | None = None,
                           root: pathlib.Path | None = None) -> None:
    """Charge failures by default and synchronize BLOCKED state/checkpoint."""
    worker = dict(worker)
    worker.update({"role": role, "pid": pid, "status": "failed"})
    release_infrastructure_attempt(
        state, role, worker, failure_class, root or p["base"].parent,
    )
    state["worker"] = worker
    if state["workflow_state"] in {"IMPLEMENTING", "ASSESSING", "VERIFYING"}:
        record_transition(state, "BLOCKED", reason)
    try:
        save_state(p, state, mirror=False, owner_generation=generation)
        write_checkpoint(p, state, role, "failed", pid, report,
                         "recover", generation)
    except BaseException:
        invalidate_checkpoint(p)
        raise


def mark_worker_blocked(p: dict[str, pathlib.Path], role: str,
                        proc: subprocess.Popen[str], reason: str, generation: str,
                        *, failure_class: str | None = None) -> None:
    with state_lock(p):
        state = load_state(p, repair_mirror=False)
        assert_generation(state, generation, role)
        worker = dict(state.get("worker") or {})
        report = pathlib.Path(worker.get("expected_output") or
                              (p["runtime"] / f"{role}-{generation}.json"))
        persist_blocked_worker(p, state, role, worker, reason,
                               generation, report, proc.pid,
                               failure_class=failure_class)


def cgroup_exec(args: argparse.Namespace) -> None:
    cgroup = validate_worker_cgroup(pathlib.Path(args.cgroup))
    command = list(args.argv)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise HarnessError("cgroup worker command is empty")
    try:
        (cgroup / "cgroup.procs").write_text(str(os.getpid()), encoding="ascii")
    except OSError as exc:
        raise HarnessError(f"cannot enter worker cgroup: {type(exc).__name__}") from exc
    os.execvpe(command[0], command, os.environ)


def run_worker(args: argparse.Namespace, role: str) -> None:
    root = git_root(True)
    p = paths(root)
    cfg = load_config(p)
    max_attempts = int(cfg.get("max_slice_attempts", 3))
    suffix = "-dry-run" if args.dry_run else ""
    report = p["runtime"] / f"{role}{suffix}.json"
    prompt_file = p["runtime"] / f"{role}{suffix}-prompt.md"
    log_file = p["runtime"] / f"{role}{suffix}.log"
    command_file = p["runtime"] / f"{role}{suffix}-command.json"
    proc: subprocess.Popen[str] | None = None
    worker_cgroup: pathlib.Path | None = None
    log_handle = None
    generation = ""
    with state_lock(p):
        state = load_state(p)
        if args.dry_run:
            prompt = render_prompt(root, p, cfg, state, role)
            atomic_runtime_write(prompt_file, prompt)
            argv = agent_adapter_argv(
                cfg, root, role, prompt_file, schema_path(role), report
            )
            models, reasoning_efforts = runtime_role_routing(cfg["agent_runtime"])
            sandbox = "workspace-write" if role == "implementer" else "read-only"
            atomic_runtime_json(command_file, {"argv": argv, "sandbox": sandbox,
                                               "ephemeral": True, "dry_run": True,
                                               "requested_model": models.get(role, ""),
                                               "requested_reasoning_effort": reasoning_efforts.get(role, "")})
            atomic_runtime_write(log_file, "SYNTHETIC WORKER OUTPUT ISOLATION CHECK\n")
            atomic_runtime_json(report, {"dry_run": True, "would_launch": argv,
                                         "workflow_state_unchanged": state["workflow_state"]})
            emit(f"run-{role}", "dry-run", state=state["workflow_state"],
                 sandbox=sandbox, report=str(report.relative_to(root)))
            return
        require_v1_active_state(state)
        if role == "implementer":
            allowed = {"SLICE_READY", "CHANGES_REQUIRED"}
        elif role == "reviewer":
            allowed = {"VALIDATING", "ASSESSING"}
        elif role in {"tester", "security_reviewer"}:
            allowed = {"ASSESSING"}
        else:
            allowed = {"ASSESSING"}
        if state["workflow_state"] not in allowed:
            raise HarnessError(f"run-{role} not allowed from {state['workflow_state']}")
        completed = state.get("completed_assessments", [])
        if role == "reviewer" and state["workflow_state"] == "ASSESSING" and role in completed:
            raise HarnessError("reviewer already has accepted evidence for this candidate")
        if role != "implementer" and not state.get("gates_passed"):
            raise HarnessError(f"{role} requires passing deterministic gates")
        if role != "implementer" and state.get("gate_level") not in {"slice", "stage"}:
            raise HarnessError(f"{role} requires a passing slice or stage gate")
        if role == "tester" and "reviewer" not in completed:
            raise HarnessError("tester requires a completed Correctness Reviewer report")
        if role == "security_reviewer":
            if "security_reviewer" not in state.get("required_assessments", []):
                raise HarnessError("security reviewer is required only by SECURITY workflow")
            if "tester" not in completed:
                raise HarnessError("security reviewer requires a completed Tester report")
        if role == "verifier":
            missing = set(state.get("required_assessments", [])) - set(completed)
            if missing:
                raise HarnessError(f"verifier requires all assessor evidence; missing {sorted(missing)}")
            trusted_keys = {"reviewer": "trusted_review", "tester": "trusted_test",
                            "security_reviewer": "trusted_security_review"}
            for assessor in state.get("required_assessments", []):
                assessor_report = validate_report(p[trusted_keys[assessor]], schema_path(assessor))
                require_candidate_binding(root, state, assessor_report, assessor)
                if assessor == "tester" and assessor_report.get("outcome") == "flaky_or_infra":
                    raise HarnessError(
                        "verifier cannot proceed after flaky_or_infra required testing"
                    )
        enable_child_subreaper()
        existing_owner = state.get("owner")
        if owner_alive(existing_owner):
            raise HarnessError("another live Harness owner already controls this project")
        if role == "implementer" and int(state.get("attempt", 0)) >= max_attempts:
            transition(p, state, "HUMAN_CHECKPOINT", "Slice attempt limit exceeded")
            raise HarnessError("max_slice_attempts exceeded; HUMAN_CHECKPOINT required")
        preflight = run_agent_preflight(cfg, root)
        generation = secrets.token_hex(16)
        if role == "implementer":
            clear_trusted_evidence(p)
            state["worker_run_seq"] = int(state.get("worker_run_seq", 0)) + 1
            state["attempt"] = int(state.get("attempt", 0)) + 1
            state["gates_passed"] = False
            state["gate_level"] = None
            state["completed_assessments"] = []
            state["tester_outcome"] = None
            state["assessor_findings"] = {}
            state["unresolved_findings"] = []
            state["evidence_counters"] = {}
            state["verifier_state"] = "pending"
            state["base_sha"] = git_head(root)
            state["candidate_id"] = None
            state["validated_worktree"] = None
            launch_worktree_fingerprint = worktree_fingerprint(root)
            transition(p, state, "IMPLEMENTING", "Implementer launch")
        elif role == "reviewer":
            state["worker_run_seq"] = int(state.get("worker_run_seq", 0)) + 1
            if state["workflow_state"] == "VALIDATING":
                transition(p, state, "ASSESSING", "independent Correctness Reviewer launch")
            else:
                save_state(p, state)
        elif role == "verifier":
            state["worker_run_seq"] = int(state.get("worker_run_seq", 0)) + 1
            transition(p, state, "VERIFYING", "independent Verifier launch")
        else:
            state["worker_run_seq"] = int(state.get("worker_run_seq", 0)) + 1
        state = load_state(p)
        try:
            worker_cgroup = create_worker_cgroup()
        except HarnessError:
            worker_cgroup = None
        report = p["runtime"] / f"{role}-{generation}.json"
        state["owner"] = {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                          "generation": generation, "claimed_at": utc_now()}
        state["worker"] = {"role": role, "pid": None, "status": "launching",
                           "process_identity": None,
                           "cgroup": str(worker_cgroup) if worker_cgroup else None,
                           "generation": generation, "stage_id": state.get("stage_id"),
                           "slice_id": state.get("slice_id"), "attempt": state.get("attempt"),
                           "base_sha": state.get("base_sha"), "candidate_id": state.get("candidate_id"),
                           "launch_worktree_fingerprint": (
                               launch_worktree_fingerprint if role == "implementer" else None
                           ),
                           "preflight": preflight,
                           "business_attempt_committed": role == "implementer",
                           "expected_output": str(report), "started_at": utc_now()}
        save_state(p, state, mirror=False)
        write_checkpoint(p, state, role, "launching", None, report,
                         "run-gates" if role == "implementer" else "approve-slice",
                         generation, mirror=False)
        prompt = render_prompt(root, p, cfg, state, role)
        atomic_runtime_write(prompt_file, prompt)
        runtime_unlink(report)
        argv = agent_adapter_argv(
            cfg, root, role, prompt_file, schema_path(role), report
        )
        models, reasoning_efforts = runtime_role_routing(cfg["agent_runtime"])
        sandbox = "workspace-write" if role == "implementer" else "read-only"
        atomic_runtime_json(command_file, {"argv": argv, "sandbox": sandbox, "ephemeral": True,
                                           "generation": generation,
                                           "requested_model": models.get(role, ""),
                                           "requested_reasoning_effort": reasoning_efforts.get(role, ""),
                                           "containment": "cgroup+process-group" if worker_cgroup else "process-group",
                                           "trusted_local": "project code executes as the current Unix user",
                                           "cgroup": str(worker_cgroup) if worker_cgroup else None})
        log_handle = secure_log_file(log_file)
        launch_argv = ([sys.executable, str(pathlib.Path(__file__).resolve()), "_cgroup-exec",
                        str(worker_cgroup), "--", *argv] if worker_cgroup else argv)
        with prompt_file.open("r", encoding="utf-8") as stdin:
            try:
                proc = subprocess.Popen(launch_argv, cwd=root, stdin=stdin, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, start_new_session=True,
                                        env=worker_environment())
            except OSError as exc:
                if worker_cgroup is not None:
                    destroy_worker_cgroup(worker_cgroup, kill=True)
                log_handle.close()
                log_handle = None
                persist_blocked_worker(
                    p, state, role, state["worker"], f"failed to start {ROLE_LABELS[role]}",
                    generation, report, None, failure_class="spawn_exec_failure",
                    root=root,
                )
                raise HarnessError(concise(str(exc), int(cfg.get("error_excerpt_limit", 1200)))) from exc
            identity = process_identity(proc.pid)
            state["worker"] = {"role": role, "pid": proc.pid, "status": "running",
                               "process_identity": identity, "expected_output": str(report),
                               "cgroup": str(worker_cgroup) if worker_cgroup else None,
                               "generation": generation, "stage_id": state.get("stage_id"),
                               "slice_id": state.get("slice_id"), "attempt": state.get("attempt"),
                               "base_sha": state.get("base_sha"), "candidate_id": state.get("candidate_id"),
                               "launch_worktree_fingerprint": state["worker"].get(
                                   "launch_worktree_fingerprint"
                               ),
                               "preflight": state["worker"].get("preflight"),
                               "business_attempt_committed": role == "implementer",
                               "started_at": utc_now()}
            try:
                save_state(p, state, mirror=False, owner_generation=generation)
                write_checkpoint(p, state, role, "running", proc.pid, report,
                                 "run-gates" if role == "implementer" else "approve-slice",
                                 generation, mirror=False)
            except BaseException as exc:
                terminate_worker_tree(proc, cgroup=worker_cgroup)
                log_handle.close()
                log_handle = None
                persist_blocked_worker(
                    p, state, role, state["worker"],
                    f"failed to persist {ROLE_LABELS[role]} launch",
                    generation, report, proc.pid,
                )
                if isinstance(exc, HarnessError):
                    raise
                raise HarnessError(
                    f"failed to persist {ROLE_LABELS[role]} launch: {type(exc).__name__}"
                ) from exc

    # Runtime wait is intentionally outside the state lock. This keeps status,
    # context maintenance, and recovery responsive while workflow_state still
    # prevents a duplicate worker launch.
    if proc is not None:
        if proc.stdout is None or log_handle is None:
            raise HarnessError("agent runtime output pipe was not created")
        pump: threading.Thread | None = None
        try:
            stream_errors: list[BaseException] = []

            def pump_output() -> None:
                try:
                    stream_redacted(proc.stdout, log_handle)
                except BaseException as exc:
                    stream_errors.append(exc)

            pump = threading.Thread(target=pump_output, name=f"harness-{role}-output", daemon=True)
            pump.start()
            deadline = time.monotonic() + int(cfg.get("worker_timeout_seconds", 3600))
            timed_out = False
            while proc.poll() is None and not stream_errors:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.02)
            if stream_errors or timed_out:
                terminate_worker_tree(proc, cgroup=worker_cgroup)
            else:
                proc.wait()
                terminate_worker_tree(proc, parent_waited=True, cgroup=worker_cgroup)
            pump.join(timeout=2.0)
            if pump.is_alive():
                with contextlib.suppress(Exception):
                    proc.stdout.close()
                pump.join(timeout=0.5)
            if pump.is_alive():
                raise HarnessError(f"{ROLE_LABELS[role]} output pump did not terminate")
            proc.stdout.close()
            if stream_errors:
                raise stream_errors[0]
            if timed_out:
                raise HarnessError(f"{ROLE_LABELS[role]} exceeded total worker timeout")
            os.fsync(log_handle.fileno())
            log_handle.close()
            log_handle = None
            rc = int(proc.returncode if proc.returncode is not None else 1)
        except BaseException as exc:
            terminate_worker_tree(proc, cgroup=worker_cgroup)
            with contextlib.suppress(Exception):
                proc.stdout.close()
            if pump is not None:
                pump.join(timeout=0.5)
            if log_handle is not None:
                with contextlib.suppress(Exception):
                    log_handle.close()
                log_handle = None
            reason = (f"{ROLE_LABELS[role]} timed out" if isinstance(exc, HarnessError) and
                      "exceeded total worker timeout" in str(exc) else
                      f"{ROLE_LABELS[role]} lifecycle failure")
            mark_worker_blocked(p, role, proc, reason, generation)
            if isinstance(exc, HarnessError):
                raise
            raise HarnessError(
                f"{ROLE_LABELS[role]} lifecycle failure: {type(exc).__name__}"
            ) from exc
        snapshot = load_state(p, repair_mirror=False)
        if rc != 0:
            try:
                excerpt = read_log_tail(log_file, int(cfg.get("error_excerpt_limit", 1200)))
                atomic_runtime_write(p["runtime"] / f"{role}-error-summary.txt", excerpt + "\n")
            except HarnessError:
                excerpt = f"{ROLE_LABELS[role]} exited {rc}; runtime log unavailable"
            failure_class = (
                agent_startup_failure_class(excerpt, report)
                if role == "implementer" else None
            )
            mark_worker_blocked(
                p, role, proc, f"{ROLE_LABELS[role]} exited {rc}", generation,
                failure_class=failure_class,
            )
            raise HarnessError(f"{ROLE_LABELS[role]} failed: {excerpt}")
        try:
            with state_lock(p):
                snapshot = load_state(p, repair_mirror=False)
                assert_generation(snapshot, generation, role)
                write_checkpoint(p, snapshot, role, "completed", proc.pid, report,
                                 "run-gates" if role == "implementer" else "approve-slice",
                                 generation)
        except HarnessError:
            mark_worker_blocked(
                p, role, proc, f"{ROLE_LABELS[role]} runtime integrity failure", generation
            )
            raise

    try:
        result = validate_report(report, schema_path(role))
    except HarnessError:
        if proc is not None:
            mark_worker_blocked(
                p, role, proc,
                f"{ROLE_LABELS[role]} produced an invalid structured handoff", generation,
            )
        raise
    with state_lock(p):
        state = load_state(p)
        validate_worker_handoff_or_block(
            root, p, state, result, role, generation, report,
            proc.pid if proc is not None else None,
        )
        if role == "implementer":
            state["candidate_id"] = result["candidate_id"]
        atomic_runtime_json(p["runtime"] / REPORT_FILES[role], result)
        trusted_key = {"implementer": "trusted_implementation", "reviewer": "trusted_review",
                       "tester": "trusted_test", "security_reviewer": "trusted_security_review",
                       "verifier": "trusted_verification"}[role]
        atomic_json(p[trusted_key], result)
        persist_worker_handoff(
            p, state, role, result, generation, report,
            proc.pid if proc is not None else None,
        )
    emit(f"run-{role}", "ok", state=load_state(p)["workflow_state"], report=str(report.relative_to(root)))


def configured_gate_commands(cfg: dict[str, Any], level: str) -> list[tuple[str, list[str]]]:
    configured = cfg.get("gates", {}).get(level)
    if configured is None and level == "slice":
        legacy = cfg.get("commands", {})
        configured = [legacy.get(name, []) for name in
                      ("formatter", "lint", "check", "unit_test", "integration_test")]
    if not isinstance(configured, list):
        raise HarnessError(f"gates.{level} must be an array of command arrays")
    result: list[tuple[str, list[str]]] = []
    for index, argv in enumerate(configured, 1):
        if argv == []:
            continue
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise HarnessError(f"gates.{level}[{index - 1}] must be a non-empty string array")
        result.append((f"{level}-{index}", argv))
    return result


def show_candidate_identity(args: argparse.Namespace) -> None:
    root = git_root(True)
    p = paths(root)
    state = load_state(p)
    base_sha, candidate_id = candidate_identity(root, state.get("base_sha"))
    value = {"base_sha": base_sha, "candidate_id": candidate_id,
             "attempt": state.get("attempt")}
    if args.json:
        print(json.dumps(value, separators=(",", ":")))
    else:
        emit("candidate-id", "ok", **value)


def executable_gate_command(root: pathlib.Path, argv: list[str]) -> list[str]:
    """Resolve gates without breaking argv[0]-sensitive executable proxies."""
    preserve_proxy_name = False
    if len(argv) >= 2 and argv[0] == "harness" and argv[1].startswith("_"):
        executable = skill_root() / "scripts" / "harness"
    else:
        raw = pathlib.Path(argv[0])
        if raw.is_absolute():
            executable = raw
        elif "/" in argv[0]:
            executable = root / raw
        else:
            resolved = shutil.which(argv[0])
            if not resolved:
                raise HarnessError(f"configured gate executable was not found on PATH: {argv[0]}")
            executable = pathlib.Path(resolved)
            # rustup and similar multicall proxies dispatch from argv[0]. Following
            # a PATH symlink such as ~/.cargo/bin/cargo -> rustup would execute
            # `rustup test ...` instead of `cargo test ...`.
            preserve_proxy_name = True
    executable = executable.absolute() if preserve_proxy_name else executable.resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise HarnessError(f"configured gate executable is missing or not executable: {argv[0]}")
    return [str(executable), *argv[1:]]


def run_gates(args: argparse.Namespace) -> None:
    root = git_root(True)
    p = paths(root)
    cfg = load_config(p)
    with state_lock(p):
        state = load_state(p)
        require_v1_active_state(state)
        if state["workflow_state"] != "VALIDATING":
            raise HarnessError("run-gates requires VALIDATING")
        results = []
        all_pass = True

        commands = configured_gate_commands(cfg, args.level)
        if not commands:
            all_pass = False
        with tempfile.TemporaryDirectory(prefix="harness-gate-home-") as home:
            env = gate_environment(pathlib.Path(home))
            for name, argv in commands:
                log_path = p["runtime"] / f"gate-{name}.log"
                resolved_argv = executable_gate_command(root, argv)
                return_code, timed_out = run_logged(
                    resolved_argv, root, log_path,
                    int(cfg.get("gate_timeout_seconds", 1800)), env,
                )
                status_value = "passed" if return_code == 0 else "failed"
                results.append({"name": name, "status": status_value, "command": argv,
                                "resolved_command": resolved_argv,
                                "exit_code": return_code,
                                "timed_out": timed_out,
                                "log": str(log_path.relative_to(root))})
                if return_code != 0:
                    all_pass = False
                    break
        failure_class = "none" if all_pass else "product"
        base_sha, candidate_id = candidate_identity(root, state.get("base_sha"))
        report = {"schema_version": "1.0", "status": "passed" if all_pass else "failed", "level": args.level,
                  "base_sha": base_sha, "candidate_id": candidate_id,
                  "failure_class": failure_class,
                  "gates": results,
                  "completed_at": utc_now()}
        atomic_runtime_json(p["runtime"] / REPORT_FILES["gates"], report)
        validate_report(p["runtime"] / REPORT_FILES["gates"], skill_root() / "schemas" / "quality-gates.schema.json")
        atomic_json(p["trusted_gates"], report)
        state["gates_passed"] = all_pass
        state["gate_level"] = args.level if all_pass else None
        if all_pass:
            state["validated_worktree"] = worktree_fingerprint(root)
            state["candidate_id"] = candidate_id
            save_state(p, state)
        else:
            state["validated_worktree"] = None
            transition(p, state, "CHANGES_REQUIRED", "deterministic quality gate failed")
    emit("run-gates", "ok" if all_pass else "failed", state=load_state(p)["workflow_state"],
         level=args.level, gates=len(results))
    if not all_pass:
        raise HarnessError("quality gate failed; inspect the named runtime log deliberately")


def gofmt_check(args: argparse.Namespace) -> None:
    root = git_root(True)
    files: list[str] = []
    requested = args.files or sorted(
        str(path.relative_to(root)) for path in root.rglob("*.go")
        if ".git" not in path.parts and ".harness" not in path.parts
    )
    for raw in requested:
        path = (root / raw).resolve()
        if not path.is_relative_to(root.resolve()) or path.suffix != ".go" or not path.is_file():
            raise HarnessError("_gofmt-check accepts only existing .go files inside the Git project")
        files.append(str(path.relative_to(root)))
    if not files:
        raise HarnessError("_gofmt-check requires at least one Go file")
    cp = run_capture(["gofmt", "-l", *files], cwd=root)
    if cp.returncode != 0:
        raise HarnessError("gofmt failed")
    unformatted = [line for line in cp.stdout.splitlines() if line.strip()]
    if unformatted:
        raise HarnessError(f"gofmt reported {len(unformatted)} unformatted file(s)")


def rust_affected_check(_args: argparse.Namespace) -> None:
    root = git_root(True)
    cp = run_capture(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root)
    if cp.returncode != 0:
        raise HarnessError("cannot determine affected Rust crates")
    members: set[str] = set()
    workspace_wide = False
    for token in cp.stdout.split("\0"):
        if not token:
            continue
        relative = token[3:] if len(token) >= 3 and token[2] == " " else token
        parts = pathlib.PurePosixPath(relative).parts
        if len(parts) >= 2 and parts[0] == "crates":
            manifest = root / "crates" / parts[1] / "Cargo.toml"
            if manifest.is_file() and tomllib is not None:
                with manifest.open("rb") as handle:
                    name = tomllib.load(handle).get("package", {}).get("name")
                if isinstance(name, str):
                    members.add(name)
        elif relative in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "rustfmt.toml"}:
            workspace_wide = True
    commands = [["git", "diff", "--check"], ["cargo", "fmt", "--all", "--check"]]
    if workspace_wide:
        commands.append(["cargo", "check", "--locked", "--workspace", "--all-targets", "--all-features"])
    elif members:
        package_args = [item for name in sorted(members) for item in ("-p", name)]
        commands.append(["cargo", "check", "--locked", *package_args, "--all-targets", "--all-features"])
        commands.append(["cargo", "test", "--locked", *package_args])
    for argv in commands:
        cp = subprocess.run(argv, cwd=root, env=os.environ, check=False)
        if cp.returncode != 0:
            raise HarnessError(f"affected-crate gate failed: {argv[0]} {argv[1]}")


def completed_stage_document(state: dict[str, Any], implementation: dict[str, Any],
                             gates: dict[str, Any], verification: dict[str, Any],
                             completed_at: str) -> str:
    return (
        f"# Current Stage\n\n- Stage: {state.get('stage_id')}\n"
        f"- Title: {state.get('stage_title')}\n"
        f"- Final Slice: {state.get('slice_id')}\n"
        f"- Status: Completed\n- Completed: {completed_at}\n\n"
        "## Canonical completion evidence\n\n"
        f"- Implementer handoff: {implementation['status']}\n"
        f"- Deterministic gate: {gates['status']} ({gates['level']})\n"
        f"- Required assessments: {', '.join(state.get('required_assessments', []))}\n"
        f"- Independent verifier: {verification['decision']}\n"
        f"- Candidate: {state.get('candidate_id')}\n\n"
        "Detailed acceptance evidence remains in the repository Execution Plan history.\n"
    )


def completion_timestamp(p: dict[str, pathlib.Path], state: dict[str, Any]) -> str:
    """Reuse a crash-partial completion timestamp for idempotent approval retry."""
    if p["trusted_stage"].exists():
        existing = read_single_link_text(p["trusted_stage"], "trusted current Stage")
        identity = (
            f"- Stage: {state.get('stage_id')}",
            f"- Final Slice: {state.get('slice_id')}",
            "- Status: Completed",
        )
        if all(item in existing for item in identity):
            match = re.search(r"(?m)^- Completed: ([0-9T:+.\-]{10,40})$", existing)
            if match:
                return match.group(1)
    return utc_now()


def approve_slice(args: argparse.Namespace) -> None:
    root = git_root(True)
    p = paths(root)
    cfg = load_config(p)
    with state_lock(p):
        state = load_state(p)
        require_v1_active_state(state)
        if state["workflow_state"] != "APPROVED":
            raise HarnessError("approve-slice requires APPROVED verifier state")
        if not state.get("gates_passed") or state.get("verifier_state") != "approved":
            raise HarnessError("approval requires passing gates and Verifier approval")
        required_level = "stage" if args.complete_stage else "slice"
        accepted_levels = {"stage"} if required_level == "stage" else {"slice", "stage"}
        if state.get("gate_level") not in accepted_levels:
            raise HarnessError(f"approval requires a passing {required_level} gate")
        implementation = validate_report(p["trusted_implementation"], schema_path("implementer"))
        gates = validate_report(p["trusted_gates"],
                                skill_root() / "schemas" / "quality-gates.schema.json")
        review = validate_report(p["trusted_review"], schema_path("reviewer"))
        test = validate_report(p["trusted_test"], schema_path("tester"))
        reports = [review, test]
        if "security_reviewer" in state.get("required_assessments", []):
            reports.append(validate_report(p["trusted_security_review"], schema_path("security_reviewer")))
        verification = validate_report(p["trusted_verification"], schema_path("verifier"))
        for role, report in [("implementer", implementation), ("gates", gates),
                             ("reviewer", review), ("tester", test), ("verifier", verification)]:
            require_candidate_binding(root, state, report, role)
        if len(reports) == 3:
            require_candidate_binding(root, state, reports[2], "security_reviewer")
        if (implementation["status"] != "completed" or gates["status"] != "passed"
                or any(report["status"] != "completed" for report in reports)
                or verification["decision"] != "approved"):
            raise HarnessError("structured evidence does not satisfy Slice approval")
        if test.get("outcome") == "flaky_or_infra":
            raise HarnessError("flaky_or_infra required testing cannot satisfy Slice approval")
        require_protected_unchanged(root, cfg, state)
        if state.get("validated_worktree") != worktree_fingerprint(root):
            raise HarnessError("worktree changed after deterministic gates; rerun gates and review")
        plan = ""
        if args.next_slice:
            if not args.plan_file:
                raise HarnessError("--next-slice requires --plan-file with approved acceptance criteria")
            plan = read_approved_plan(root, args.plan_file)
        if args.complete_stage:
            completed_at = completion_timestamp(p, state)
            completed_stage = completed_stage_document(
                state, implementation, gates, verification, completed_at
            )
            atomic_write(p["stage"], completed_stage)
            atomic_write(p["trusted_stage"], completed_stage, mode=0o600)
            record_transition(state, "STAGE_COMPLETED", "all Stage acceptance criteria satisfied")
            write_project_current_status(p, state)
            project = read_single_link_text(p["project"], "project state")
            marker = f"## Completed Stage {state.get('stage_id')}"
            if marker not in project:
                atomic_write(p["project"], project.rstrip() + f"\n\n{marker}\n\nCompleted: {completed_at}\n")
            save_state(p, state)
            clear_runtime(p, preserve_lock=True)
        elif args.next_slice:
            state["slice_id"] = args.next_slice
            state["attempt"] = 0
            state["gates_passed"] = False
            state["gate_level"] = None
            state["completed_assessments"] = []
            state["tester_outcome"] = None
            state["verifier_state"] = "pending"
            state["base_sha"] = None
            state["candidate_id"] = None
            state["worker"] = None
            state["validated_worktree"] = None
            body = (f"# Current Stage\n\n- Stage: {state.get('stage_id')}\n"
                    f"- Title: {state.get('stage_title')}\n- Current Slice: {args.next_slice}\n"
                    f"- Approved: {utc_now()}\n\n## Plan and acceptance criteria\n\n{plan.rstrip()}\n")
            atomic_write(p["stage"], body)
            atomic_write(p["trusted_stage"], body, mode=0o600)
            record_transition(state, "SLICE_READY", "next approved Slice ready")
            write_project_current_status(p, state)
            state["protected_baseline"] = protected_snapshot(root, cfg)
            save_state(p, state)
            clear_trusted_evidence(p)
        else:
            raise HarnessError("approval target is required")
    emit("approve-slice", "ok", state=load_state(p)["workflow_state"], slice=load_state(p).get("slice_id"))


def clear_runtime(p: dict[str, pathlib.Path], preserve_lock: bool = False) -> None:
    del preserve_lock  # The state lock lives in trusted Git metadata, never runtime.
    directory_fd = open_directory_nofollow(p["runtime"])
    try:
        clear_directory_fd(directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    with contextlib.suppress(FileNotFoundError):
        p["trusted_checkpoint"].unlink()


def reset_runtime(_args: argparse.Namespace) -> None:
    root = git_root(True)
    p = paths(root)
    if not p["base"].is_dir():
        raise HarnessError("project is not initialized")
    with state_lock(p):
        state = load_state(p)
        if state["workflow_state"] not in {"STAGE_DISCUSSION", "STAGE_COMPLETED"}:
            raise HarnessError("reset-runtime is allowed only between active Stages")
        clear_runtime(p, preserve_lock=True)
    emit("reset-runtime", "ok", preserved=["config.toml", "state.json", "PROJECT_STATE.md", "CURRENT_STAGE.md"])


def recover(args: argparse.Namespace) -> None:
    root = git_root(True)
    reopen_review = getattr(args, "reopen_review", False)
    reopen_reason = str(getattr(args, "reason", "") or "").strip()
    plan_file = getattr(args, "plan_file", None)
    if plan_file and not args.ack_human:
        raise HarnessError("plan amendment requires --ack-human")
    amended_plan = read_approved_plan(root, plan_file) if plan_file else None
    if amended_plan is not None and len(re.findall(r"[A-Za-z]", amended_plan)) < 20:
        raise HarnessError("the amended Stage plan must be an English engineering artifact")
    p = paths(root, allow_unsafe_runtime=True)
    cfg = load_config(p)
    with state_lock(p):
        state = load_state(p, repair_mirror=not p["base"].is_symlink())
        runtime_safe = not p["runtime"].is_symlink()
        checkpoint_path = (p["trusted_checkpoint"] if p["trusted_checkpoint"].exists()
                           else p["checkpoint"] if runtime_safe else p["trusted_checkpoint"])
        checkpoint = load_json(checkpoint_path) if checkpoint_path.exists() else None
        worker = state.get("worker") or {}
        owner = state.get("owner") or {}
        active_pid = checkpoint.get("process_id") if checkpoint else worker.get("pid")
        active_identity = (checkpoint.get("process_identity") if checkpoint
                           else worker.get("process_identity"))
        cgroup_value = (checkpoint.get("worker_cgroup") if checkpoint else None) or worker.get("cgroup")
        worker_cgroup = pathlib.Path(cgroup_value) if isinstance(cgroup_value, str) and cgroup_value else None
        live_owner = owner_alive(owner)
        if reopen_review:
            if state["workflow_state"] != "APPROVED" or state.get("verifier_state") != "approved":
                raise HarnessError("recover --reopen-review requires APPROVED verifier evidence")
            if not reopen_reason:
                raise HarnessError("recover --reopen-review requires --reason")
            if live_owner:
                raise HarnessError("cannot reopen review while the recorded Harness owner is still alive")
            state["gates_passed"] = False
            state["gate_level"] = None
            state["completed_assessments"] = []
            state["tester_outcome"] = None
            state["verifier_state"] = "pending"
            state["validated_worktree"] = None
            state["owner"] = None
            state["worker"] = None
            state["unresolved_findings"] = [{
                "severity": "medium",
                "file": "<orchestrator>",
                "line": 1,
                "message": reopen_reason,
            }]
            clear_trusted_evidence(p)
            transition(p, state, "CHANGES_REQUIRED", "operator reopened approved review")
            emit("recover", "ok", state="CHANGES_REQUIRED")
            return
        if state["workflow_state"] == "STAGE_COMPLETED" and not args.retry and not args.ack_human:
            current = read_single_link_text(p["stage"], "current Stage") if p["stage"].exists() else ""
            trusted = (read_single_link_text(p["trusted_stage"], "trusted current Stage")
                       if p["trusted_stage"].exists() else "")
            if "- Status: Completed" not in current or "- Status: Completed" not in trusted:
                implementation = validate_report(p["trusted_implementation"], schema_path("implementer"))
                gates = validate_report(
                    p["trusted_gates"], skill_root() / "schemas" / "quality-gates.schema.json"
                )
                verification = validate_report(p["trusted_verification"], schema_path("verifier"))
                if (implementation["status"] != "completed" or gates["status"] != "passed"
                        or verification["decision"] != "approved"):
                    raise HarnessError("cannot repair completed Stage without valid canonical evidence")
                completed_at = next(
                    (item.get("at") for item in reversed(state.get("recent_transitions", []))
                     if item.get("to") == "STAGE_COMPLETED" and item.get("at")),
                    state.get("updated_at") or utc_now(),
                )
                body = completed_stage_document(
                    state, implementation, gates, verification, str(completed_at)
                )
                atomic_write(p["stage"], body)
                atomic_write(p["trusted_stage"], body, mode=0o600)
                emit("recover", "repaired", state="STAGE_COMPLETED",
                     artifact="CURRENT_STAGE.md")
                return
            emit("recover", "noop", state="STAGE_COMPLETED")
            return
        if args.retry:
            if (state["workflow_state"] == "CHANGES_REQUIRED"
                    and state.get("verifier_state") == "pending"
                    and set(state.get("required_assessments", []))
                    <= set(state.get("completed_assessments", []))):
                transition(p, state, "ASSESSING", "retry verifier after invalid handoff")
                emit("recover", "ok", state="ASSESSING")
                return
            if state["workflow_state"] != "BLOCKED":
                raise HarnessError("recover --retry requires BLOCKED")
            if live_owner:
                raise HarnessError("cannot retry while the recorded Harness owner is still alive")
            retry_cgroup_value = worker.get("cgroup")
            retry_cgroup = (pathlib.Path(retry_cgroup_value)
                            if isinstance(retry_cgroup_value, str) and retry_cgroup_value else None)
            if retry_cgroup is not None:
                destroy_worker_cgroup(retry_cgroup, kill=True)
            elif process_alive(worker.get("pid"), worker.get("process_identity")):
                raise HarnessError("cannot retry while the recorded worker process is still alive")
            infrastructure_retry = state.get("tester_outcome") == "flaky_or_infra"
            assessor_role = worker.get("role")
            assessor_retry = assessor_role in {"reviewer", "tester", "security_reviewer"}
            verifier_retry = worker.get("role") == "verifier" and worker.get("status") == "failed"
            target = ("ASSESSING" if infrastructure_retry or assessor_retry or verifier_retry else
                      "CHANGES_REQUIRED" if state.get("attempt", 0) else "SLICE_READY")
            state["owner"] = None
            state["worker"] = None
            if infrastructure_retry or assessor_retry:
                role_to_retry = "tester" if infrastructure_retry else str(assessor_role)
                clear_assessor_retry_evidence(p, state, role_to_retry)
            elif verifier_retry:
                runtime_unlink(p["trusted_verification"])
                runtime_unlink(p["runtime"] / REPORT_FILES["verifier"])
                invalidate_checkpoint(p)
            transition(p, state, target, "human authorized recovery retry")
            emit("recover", "ok", state=target)
            return
        if args.ack_human:
            if state["workflow_state"] != "HUMAN_CHECKPOINT":
                raise HarnessError("recover --ack-human requires HUMAN_CHECKPOINT")
            if live_owner:
                raise HarnessError("cannot acknowledge while the recorded Harness owner is still alive")
            drain_recorded_worker(worker, "acknowledge")
            state["attempt"] = 0
            state["checkpoint_epoch"] = int(state.get("checkpoint_epoch", 0)) + 1
            state["gates_passed"] = False
            state["completed_assessments"] = []
            state["tester_outcome"] = None
            state["verifier_state"] = "pending"
            state["gate_level"] = None
            state["owner"] = None
            state["worker"] = None
            if amended_plan is not None:
                body = (
                    "# Current Stage\n\n"
                    f"- Stage: {state.get('stage_id')}\n"
                    f"- Title: {state.get('stage_title')}\n"
                    f"- Current Slice: {state.get('slice_id')}\n"
                    f"- Amended at Human Checkpoint: {utc_now()}\n\n"
                    "## Plan and acceptance criteria\n\n"
                    f"{amended_plan.rstrip()}\n"
                )
                atomic_write(p["stage"], body)
                atomic_write(p["trusted_stage"], body, mode=0o600)
                clear_trusted_evidence(p)
                record_transition(
                    state, "SLICE_READY", "human checkpoint acknowledged with amended plan"
                )
                write_project_current_status(p, state)
                state["protected_baseline"] = protected_snapshot(root, cfg)
                save_state(p, state)
            else:
                transition(p, state, "SLICE_READY", "human checkpoint acknowledged")
            emit("recover", "ok", state="SLICE_READY")
            return
        active_states = {"IMPLEMENTING", "ASSESSING", "VERIFYING"}
        if state["workflow_state"] not in active_states | {"BLOCKED", "HUMAN_CHECKPOINT"}:
            emit("recover", "noop", state=state["workflow_state"])
            return
        if live_owner:
            emit("recover", "running", role=(checkpoint or worker).get("worker_role", worker.get("role")),
                 pid=active_pid, owner_pid=owner.get("pid"))
            return
        role = checkpoint.get("worker_role") if checkpoint else worker.get("role")
        binding_ok = bool(
            checkpoint and role in {"implementer", "reviewer", "tester", "security_reviewer", "verifier"} and
            checkpoint.get("stage_id") == state.get("stage_id") and
            checkpoint.get("slice_id") == state.get("slice_id") and
            checkpoint.get("attempt") == state.get("attempt") and
            checkpoint.get("base_sha") == state.get("base_sha") and
            checkpoint.get("candidate_id") == state.get("candidate_id") and
            checkpoint.get("generation") == worker.get("generation") and
            checkpoint.get("generation") == owner.get("generation") and
            checkpoint.get("owner_pid") == owner.get("pid") and
            checkpoint.get("owner_identity") == owner.get("identity") and
            worker.get("role") == role and worker.get("stage_id") == state.get("stage_id") and
            worker.get("slice_id") == state.get("slice_id") and
            worker.get("attempt") == state.get("attempt") and
            worker.get("base_sha") == state.get("base_sha") and
            worker.get("candidate_id") == state.get("candidate_id")
        )
        if checkpoint is not None and not binding_ok:
            if state["workflow_state"] in active_states:
                actual_role = worker.get("role")
                actual_cgroup = worker.get("cgroup")
                if isinstance(actual_cgroup, str) and actual_cgroup:
                    destroy_worker_cgroup(pathlib.Path(actual_cgroup), kill=True)
                elif process_alive(worker.get("pid"), worker.get("process_identity")):
                    emit("recover", "running", role=actual_role, pid=worker.get("pid"))
                    return
                blocked_worker = dict(worker)
                blocked_worker["status"] = "failed"
                blocked_worker["business_attempt_committed"] = actual_role == "implementer"
                state["worker"] = blocked_worker
                record_transition(state, "BLOCKED", "stale or mismatched worker checkpoint rejected")
                save_state(p, state, mirror=False)
            invalidate_checkpoint(p)
            emit("recover", "blocked", state=state["workflow_state"], error="checkpoint binding mismatch")
            return
        if worker_cgroup is not None:
            destroy_worker_cgroup(worker_cgroup, kill=True)
        elif process_alive(active_pid, active_identity):
            emit("recover", "running", role=role, pid=active_pid)
            return
        output = pathlib.Path(checkpoint["expected_output_file"]) if checkpoint and checkpoint.get("expected_output_file") else None
        expected_output = worker.get("expected_output")
        output_ok = bool(output and expected_output == str(output) and
                         output.parent == p["runtime"] and output.name == f"{role}-{worker.get('generation')}.json")
        if role in {"implementer", "reviewer", "tester", "security_reviewer", "verifier"} and output_ok:
            try:
                result = validate_report(output, schema_path(role))
            except HarnessError as exc:
                if state["workflow_state"] in active_states:
                    persist_blocked_worker(
                        p, state, role, worker,
                        "worker stopped with an invalid structured handoff",
                        str(worker.get("generation")), output, active_pid,
                    )
                emit("recover", "blocked", state=load_state(p)["workflow_state"],
                     error=concise(str(exc), int(cfg.get("error_excerpt_limit", 1200))))
                return
            if state["workflow_state"] in {"BLOCKED", "HUMAN_CHECKPOINT"}:
                # Recovery never jumps from a terminal checkpoint. Report evidence for a human decision.
                emit("recover", "handoff-found", state=state["workflow_state"], report=str(output.relative_to(root)))
                return
            recovery_generation = str(worker.get("generation"))
            state["owner"] = {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                              "generation": recovery_generation, "claimed_at": utc_now(),
                              "recovered_generation": worker.get("generation")}
            state["worker"] = {**worker, "generation": recovery_generation,
                               "status": "recovered", "recovered_at": utc_now()}
            try:
                require_candidate_binding(root, state, result, role)
                if role == "implementer":
                    state["candidate_id"] = result["candidate_id"]
                trusted_key = {"implementer": "trusted_implementation", "reviewer": "trusted_review",
                               "tester": "trusted_test", "security_reviewer": "trusted_security_review",
                               "verifier": "trusted_verification"}[role]
                atomic_json(p[trusted_key], result)
                atomic_runtime_json(p["runtime"] / REPORT_FILES[role], result)
                persist_worker_handoff(p, state, role, result, recovery_generation, output, None)
            except HarnessError:
                persist_blocked_worker(
                    p, state, role, worker, "stale candidate handoff rejected",
                    recovery_generation, output, active_pid,
                )
                raise
            except BaseException:
                invalidate_checkpoint(p)
                raise
            emit("recover", "ok", state=load_state(p)["workflow_state"])
            return
        if state["workflow_state"] in active_states:
            missing_report = pathlib.Path(expected_output) if isinstance(expected_output, str) else (
                p["runtime"] / f"{role}-{worker.get('generation')}.json"
            )
            persist_blocked_worker(
                p, state, role, worker, "worker stopped without a valid handoff",
                str(worker.get("generation")), missing_report, active_pid,
            )
        limit = int(cfg.get("error_excerpt_limit", 1200))
        emit("recover", "blocked", state=load_state(p)["workflow_state"], error=concise("worker stopped without valid structured output", limit))


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="harness", description="provider-neutral verified agent harness"
    )
    ap.add_argument("--version", action="version", version=f"harness {HARNESS_VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="initialize lightweight .harness project state").set_defaults(func=init_project)
    sp = sub.add_parser("doctor", help="read-only dependency checks; capability probes are opt-in")
    sp.add_argument("--capabilities", action="store_true",
                    help="run explicit process/cgroup capability probes")
    sp.set_defaults(func=doctor)
    sp = sub.add_parser("status", help="show concise workflow state")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=status)
    sp = sub.add_parser(
        "bind-session", help="bind an orchestrator session to the active worker generation"
    )
    sp.add_argument("--generation", required=True)
    sp.add_argument("--session-id", required=True)
    sp.set_defaults(func=bind_session)
    sub.add_parser("sync-config", help="explicitly import config between active Stages").set_defaults(func=sync_config)
    sub.add_parser(
        "sync-project-state",
        help="synchronize protected project-status prose from canonical state",
    ).set_defaults(func=sync_project_state)
    sp = sub.add_parser("start-stage", help="record a user-approved Stage and first Slice")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--slice", required=True)
    sp.add_argument("--plan-file", required=True,
                    help="Markdown file containing the user-approved Stage/Slice plan")
    sp.add_argument("--workflow", choices=("VERIFIED", "SECURITY"), default="VERIFIED")
    sp.set_defaults(func=start_stage)
    for name, role in (("run-implementer", "implementer"), ("run-reviewer", "reviewer"),
                       ("run-tester", "tester"),
                       ("run-security-reviewer", "security_reviewer"),
                       ("run-verifier", "verifier")):
        sp = sub.add_parser(name, help=f"run {ROLE_LABELS[role]} with isolated output")
        sp.add_argument("--dry-run", action="store_true")
        sp.set_defaults(func=lambda args, r=role: run_worker(args, r))
    sp = sub.add_parser("run-advisory", help="run one optional non-authoritative read-only advisory role")
    sp.add_argument("--role", required=True, choices=tuple(sorted(ADVISORY_ROLE_KEYS)))
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=run_advisory)
    sp = sub.add_parser("run-gates", help="run configured deterministic quality gates")
    sp.add_argument("--level", choices=("fast", "slice", "stage"), default="slice")
    sp.set_defaults(func=run_gates)
    sp = sub.add_parser("candidate-id", help="show the current content-bound candidate identity")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=show_candidate_identity)
    sp = sub.add_parser("validate-parallel-plan", help="validate a decomposition plan")
    sp.add_argument("--plan-file", required=True)
    sp.set_defaults(func=validate_parallel_plan_command)
    sp = sub.add_parser("memory-action", help="choose memory backpressure")
    sp.add_argument("--paused-count", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=memory_action_command)
    add_parallel_parsers(sub)
    sp = sub.add_parser("_gofmt-check", help=argparse.SUPPRESS)
    sp.add_argument("files", nargs="*")
    sp.set_defaults(func=gofmt_check)
    sp = sub.add_parser("_rust-affected-check", help=argparse.SUPPRESS)
    sp.set_defaults(func=rust_affected_check)
    sp = sub.add_parser("_cgroup-exec", help=argparse.SUPPRESS)
    sp.add_argument("cgroup")
    sp.add_argument("argv", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cgroup_exec)
    sp = sub.add_parser("approve-slice", help="approve after passing review and gates")
    group = sp.add_mutually_exclusive_group(required=True)
    group.add_argument("--next-slice")
    group.add_argument("--complete-stage", action="store_true")
    sp.add_argument("--plan-file", help="approved plan for --next-slice")
    sp.set_defaults(func=approve_slice)
    sub.add_parser("reset-runtime", help="clear only .harness/runtime contents").set_defaults(func=reset_runtime)
    sp = sub.add_parser("recover", help="reconcile state or resume after a human decision")
    recovery = sp.add_mutually_exclusive_group()
    recovery.add_argument("--retry", action="store_true", help="resume a BLOCKED Slice")
    recovery.add_argument("--ack-human", action="store_true", help="acknowledge HUMAN_CHECKPOINT and reset attempts")
    recovery.add_argument("--reopen-review", action="store_true", help="invalidate an approved review and resume corrections")
    sp.add_argument("--reason", help="operator reason required by --reopen-review")
    sp.add_argument("--plan-file", help="approved amended plan for --ack-human")
    sp.set_defaults(func=recover)
    return ap


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except HarnessError as exc:
        emit(args.command or "unknown", "error", error=concise(str(exc), 1200))
        return 2
    except KeyboardInterrupt:
        emit(args.command or "unknown", "interrupted")
        return 130
    except Exception as exc:
        emit(args.command or "unknown", "error", error=concise(f"{type(exc).__name__}: {exc}", 1200))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
