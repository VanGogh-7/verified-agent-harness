from __future__ import annotations


def canonical_parallel_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def parallel_plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_parallel_json(plan)).hexdigest()


def git_object_exists(root: pathlib.Path, revision: str) -> bool:
    result = run_capture(["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=root)
    return result.returncode == 0


def committed_file_digest(root: pathlib.Path, revision: str, raw_path: str) -> str:
    path = _normalized_parallel_path(raw_path, "shared contract path")
    try:
        raw = _git_bytes(root, ["show", f"{revision}:{path}"],
                         "cannot read frozen shared contract")
    except HarnessError as exc:
        raise HarnessError(f"shared contract path is absent from frozen base: {path}") from exc
    return hashlib.sha256(raw).hexdigest()


def freeze_parallel_plan(root: pathlib.Path, plan: dict[str, Any], schema_file: pathlib.Path,
                         run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id):
        raise HarnessError("parallel run ID contains unsupported characters")
    validate_parallel_plan(plan, schema_file)
    head = git_head(root)
    if plan["base_sha"] != head or not git_object_exists(root, head):
        raise HarnessError("parallel plan must freeze the current committed Git HEAD")
    status = run_capture(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root)
    if status.returncode != 0 or status.stdout.strip():
        raise HarnessError("parallel contract freeze requires a clean worktree")
    contract_paths = sorted({
        _normalized_parallel_path(path, "shared contract path")
        for contract in plan["shared_contracts"] for path in contract["paths"]
    })
    contract_digests = {
        path: committed_file_digest(root, head, path) for path in contract_paths
    }
    plan_sha = parallel_plan_digest(plan)
    contract_material = canonical_parallel_json({
        "base_sha": head, "plan_sha256": plan_sha, "contracts": contract_digests,
    })
    contract_set_id = "parallel-contract-v1:" + hashlib.sha256(contract_material).hexdigest()
    return {
        "schema_version": "1.0", "run_id": run_id, "base_sha": head,
        "plan_sha256": plan_sha, "contract_set_id": contract_set_id,
        "contract_paths": contract_paths, "contract_digests": contract_digests,
    }


def initial_parallel_state(plan: dict[str, Any], frozen: dict[str, Any]) -> dict[str, Any]:
    if frozen.get("plan_sha256") != parallel_plan_digest(plan):
        raise HarnessError("parallel plan does not match its frozen digest")
    owners = {contract["owner"] for contract in plan["shared_contracts"]}
    tasks = {task["id"]: {"status": "frozen_in_base" if task["id"] in owners else "pending",
                           "head_sha": frozen["base_sha"] if task["id"] in owners else None,
                           "changed_paths": []} for task in plan["tasks"]}
    return {"schema_version": "1.0", "run_id": frozen["run_id"],
            "base_sha": frozen["base_sha"], "plan_sha256": frozen["plan_sha256"],
            "contract_set_id": frozen["contract_set_id"], "generation": 1,
            "status": "COLLECTING_LANES", "tasks": tasks,
            "workers": {},
            "integration_order": [item for item in plan["integration"]["order"] if item not in owners],
            }


def record_parallel_lane(state: dict[str, Any], plan: dict[str, Any], task_id: str,
                         head_sha: str, changed_paths: list[str]) -> None:
    task_ids = {task["id"] for task in plan["tasks"]}
    if state.get("plan_sha256") != parallel_plan_digest(plan):
        raise HarnessError("parallel state plan digest mismatch")
    if task_id not in task_ids or task_id not in state.get("tasks", {}):
        raise HarnessError("unknown parallel task")
    owners = {contract["owner"] for contract in plan["shared_contracts"]}
    if task_id in owners:
        raise HarnessError("contract owner is already frozen in the parallel base")
    entry = state["tasks"][task_id]
    if entry.get("status") != "pending":
        raise HarnessError("parallel lane already recorded")
    entry.update({"status": "validated", "head_sha": head_sha,
                  "changed_paths": sorted(changed_paths)})

    if all(state["tasks"][item]["status"] == "validated" for item in state["integration_order"]):
        state["status"] = "READY_TO_INTEGRATE"


def parallel_integration_state(state: dict[str, Any]) -> dict[str, Any]:
    order = state.get("integration_order", [])
    pending = [item for item in order if state["tasks"][item].get("status") != "validated"]
    return {"ready": not pending, "next_task": pending[0] if pending else None,
            "ordered_heads": [state["tasks"][item]["head_sha"] for item in order
                              if state["tasks"][item].get("status") == "validated"],
            "integration_order": order}


def parallel_task_ready(state: dict[str, Any], plan: dict[str, Any], task_id: str) -> bool:
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None or state["tasks"].get(task_id, {}).get("status") != "pending":
        return False
    return all(state["tasks"].get(dep, {}).get("status") in {"validated", "frozen_in_base"}
               for dep in task["depends_on"])


def parallel_bundle(plan: dict[str, Any], freeze: dict[str, Any]) -> dict[str, Any]:
    plan_copy = json.loads(canonical_parallel_json(plan))
    freeze_copy = json.loads(canonical_parallel_json(freeze))
    return {"schema_version": "1.0", "plan": plan_copy, "freeze": freeze_copy,
            "state": initial_parallel_state(plan_copy, freeze_copy)}


def validate_parallel_bundle(root: pathlib.Path, bundle: dict[str, Any],
                             schema_file: pathlib.Path, *,
                             reconcile_paused: bool = False) -> dict[str, Any]:
    if set(bundle) != {"schema_version", "plan", "freeze", "state"}:
        raise HarnessError("parallel canonical bundle has invalid fields")
    plan = bundle["plan"]
    freeze = bundle["freeze"]
    state = bundle["state"]
    required_freeze = {"schema_version", "run_id", "base_sha", "plan_sha256",
                       "contract_set_id", "contract_paths", "contract_digests"}
    if set(freeze) != required_freeze or freeze.get("schema_version") != "1.0":
        raise HarnessError("parallel freeze schema is invalid")
    validate_parallel_plan(plan, schema_file)
    digest = parallel_plan_digest(plan)
    if digest != freeze.get("plan_sha256") or digest != state.get("plan_sha256"):
        raise HarnessError("parallel plan differs from frozen digest")
    base = freeze.get("base_sha")
    if (not isinstance(base, str) or base != plan.get("base_sha")
            or base != state.get("base_sha") or not git_object_exists(root, base)):
        raise HarnessError("parallel frozen base identity is invalid")
    expected_paths = sorted({path for contract in plan["shared_contracts"]
                             for path in contract["paths"]})
    expected_digests = {path: committed_file_digest(root, base, path) for path in expected_paths}
    if freeze.get("contract_paths") != expected_paths or freeze.get("contract_digests") != expected_digests:
        raise HarnessError("parallel frozen contract content changed")
    material = canonical_parallel_json({"base_sha": base, "plan_sha256": digest,
                                        "contracts": expected_digests})
    contract_id = "parallel-contract-v1:" + hashlib.sha256(material).hexdigest()
    if contract_id != freeze.get("contract_set_id") or contract_id != state.get("contract_set_id"):
        raise HarnessError("parallel frozen contract identity changed")
    if freeze.get("run_id") != state.get("run_id"):
        raise HarnessError("parallel run identity mismatch")
    initial = initial_parallel_state(plan, freeze)
    for key in ("schema_version", "run_id", "base_sha", "plan_sha256", "contract_set_id",
                "generation", "integration_order"):
        if state.get(key) != initial.get(key):
            raise HarnessError("parallel state immutable binding changed")
    if set(state.get("tasks", {})) != set(initial["tasks"]):
        raise HarnessError("parallel state task topology changed")
    owners = {contract["owner"] for contract in plan["shared_contracts"]}
    for task_id in owners:
        if state["tasks"][task_id] != initial["tasks"][task_id]:
            raise HarnessError("frozen contract owner state changed")
    task_map = {task["id"]: task for task in plan["tasks"]}
    required_state = {"schema_version", "run_id", "base_sha", "plan_sha256",
                      "contract_set_id", "generation", "status", "tasks",
                      "workers", "integration_order"}
    if set(state) != required_state:
        raise HarnessError("parallel state schema is invalid")
    for task_id, entry in state["tasks"].items():
        if set(entry) != {"status", "head_sha", "changed_paths"}:
            raise HarnessError("parallel task state schema is invalid")
        if task_id in owners:
            continue
        if entry.get("status") == "pending":
            if entry.get("head_sha") is not None or entry.get("changed_paths") != []:
                raise HarnessError("pending parallel lane contains unvalidated evidence")
        elif entry.get("status") == "validated":
            report = validate_parallel_lane(root, base, entry.get("head_sha"),
                                            task_map[task_id]["write_paths"])
            if report["changed_paths"] != entry.get("changed_paths"):
                raise HarnessError("validated parallel lane evidence changed")
        else:
            raise HarnessError("parallel lane has invalid status")
    validate_worker_set(state, task_map, reconcile_paused=reconcile_paused)
    for task_id, worker in state["workers"].items():
        if task_id not in task_map or state["tasks"][task_id]["status"] != "pending":
            raise HarnessError("parallel worker is not bound to a pending lane")
    expected_status = ("READY_TO_INTEGRATE" if all(
        state["tasks"][item]["status"] == "validated" for item in state["integration_order"]
    ) else "COLLECTING_LANES")
    if state["status"] != expected_status:
        raise HarnessError("parallel state status is inconsistent")
    return bundle


def git_changed_paths(root: pathlib.Path, base_sha: str, head_sha: str) -> list[str]:
    raw_output = _git_bytes(root, ["diff", "--name-only", "-z", "--no-renames",
                                    "--diff-filter=ACDMRTUXB", f"{base_sha}..{head_sha}", "--"],
                            "cannot derive lane changes from Git")
    paths: list[str] = []
    for raw in raw_output.split(b"\0"):
        if not raw:
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError("lane changed path is not valid UTF-8") from exc
        paths.append(_normalized_parallel_path(decoded, "lane changed path"))
    return sorted(set(paths))


def validate_parallel_lane(root: pathlib.Path, base_sha: str, head_sha: str,
                           write_paths: list[str]) -> dict[str, Any]:
    if not git_object_exists(root, base_sha) or not git_object_exists(root, head_sha):
        raise HarnessError("lane base or head is not a committed Git revision")
    ancestor = run_capture(["git", "merge-base", "--is-ancestor", base_sha, head_sha], cwd=root)
    if ancestor.returncode != 0:
        raise HarnessError("parallel lane head must descend from its frozen base")
    approved = [_normalized_parallel_path(path, "parallel write path") for path in write_paths]
    changed = git_changed_paths(root, base_sha, head_sha)
    outside = [path for path in changed
               if not any(_parallel_paths_overlap(path, scope) for scope in approved)]
    if outside:
        raise HarnessError(f"parallel lane changed paths outside approved write scope: {outside}")
    return {"base_sha": base_sha, "head_sha": head_sha, "changed_paths": changed,
            "write_paths": approved}


def worker_cgroup_controls_ready(cgroup: pathlib.Path) -> bool:
    return (cgroup / "cgroup.freeze").is_file() and (cgroup / "cgroup.events").is_file()


def validate_worker_binding(state: dict[str, Any], key: str, worker: dict[str, Any],
                            tasks: dict[str, dict[str, Any]], *,
                            reconcile_paused: bool = False) -> None:
    expected = state.get("workers", {}).get(key)
    required = {"id", "task_id", "run_id", "generation", "cgroup", "cgroup_device",
                "cgroup_inode", "cgroup_parent_device", "cgroup_parent_inode",
                "containment_token", "activated", "paused", "resource_class"}
    if (not isinstance(worker, dict) or set(worker) != required or expected != worker
            or key not in tasks or key != worker.get("id") or key != worker.get("task_id")
            or worker.get("run_id") != state.get("run_id")
            or worker.get("generation") != state.get("generation")
            or worker.get("resource_class") != tasks[key].get("resource_class")
            or not isinstance(worker.get("paused"), bool)
            or not isinstance(worker.get("cgroup_device"), int)
            or not isinstance(worker.get("cgroup_inode"), int)
            or not isinstance(worker.get("cgroup_parent_device"), int)
            or not isinstance(worker.get("cgroup_parent_inode"), int)
            or not isinstance(worker.get("activated"), bool)
            or not re.fullmatch(r"[0-9a-f]{32}", str(worker.get("containment_token", "")))
            or not isinstance(worker.get("cgroup"), str) or not worker["cgroup"]):
        raise HarnessError("parallel worker containment binding mismatch")
    try:
        cgroup = validate_worker_cgroup(pathlib.Path(worker["cgroup"]))
        info = cgroup.stat()
        parent_info = cgroup.parent.stat()
    except OSError as exc:
        raise HarnessError("parallel worker containment is unavailable") from exc
    if (info.st_dev != worker["cgroup_device"] or info.st_ino != worker["cgroup_inode"]
            or parent_info.st_dev != worker["cgroup_parent_device"]
            or parent_info.st_ino != worker["cgroup_parent_inode"]
            or worker["containment_token"] not in cgroup.name
            or not worker_cgroup_controls_ready(cgroup)):
        raise HarnessError("parallel worker containment stable identity mismatch")
    if worker.get("activated"):
        with open_bound_worker_cgroup(worker) as (_, cgroup_fd, _):
            events = read_cgroup_control(cgroup_fd, "cgroup.events")
        live_paused = "frozen 1" in events
        if live_paused != worker["paused"]:
            if reconcile_paused:
                worker["paused"] = live_paused
            else:
                raise HarnessError("parallel worker paused state differs from live freeze state")


def validate_worker_set(state: dict[str, Any], tasks: dict[str, dict[str, Any]], *,
                        reconcile_paused: bool = False) -> None:
    workers = state.get("workers")
    if not isinstance(workers, dict):
        raise HarnessError("parallel worker state is invalid")
    cgroups: set[str] = set()
    for key, worker in workers.items():
        validate_worker_binding(state, key, worker, tasks,
                                reconcile_paused=reconcile_paused)
        if worker["cgroup"] in cgroups:
            raise HarnessError("parallel worker cgroup identity must be unique")
        cgroups.add(worker["cgroup"])


def create_parallel_worker_containment(run_id: str, task_id: str, generation: int) -> dict[str, Any]:
    token = secrets.token_hex(16)
    parent = current_cgroup_dir()
    safe = hashlib.sha256(f"{run_id}\0{task_id}\0{generation}".encode()).hexdigest()[:16]
    cgroup = parent / f"verified-agent-harness-{safe}-{token}"
    try:
        cgroup.mkdir(mode=0o700)
        cgroup = validate_worker_cgroup(cgroup)
        if not worker_cgroup_controls_ready(cgroup):
            raise HarnessError("worker cgroup lacks complete freeze/thaw controls")
        info = cgroup.stat()
        parent_info = cgroup.parent.stat()
    except Exception:
        with contextlib.suppress(OSError):
            cgroup.rmdir()
        raise
    return {"cgroup": str(cgroup), "cgroup_device": info.st_dev,
            "cgroup_inode": info.st_ino, "cgroup_parent_device": parent_info.st_dev,
            "cgroup_parent_inode": parent_info.st_ino, "containment_token": token}


def _worker_cgroup_name(worker: dict[str, Any]) -> str:
    raw = pathlib.Path(str(worker.get("cgroup", "")))
    token = str(worker.get("containment_token", ""))
    if (not raw.is_absolute() or raw.parent == raw or not re.fullmatch(r"[0-9a-f]{32}", token)
            or token not in raw.name or not raw.name.startswith("verified-agent-harness-")):
        raise HarnessError("parallel worker containment identity is invalid")
    return raw.name


@contextlib.contextmanager
def open_bound_worker_cgroup(worker: dict[str, Any]):
    raw = pathlib.Path(str(worker.get("cgroup", "")))
    name = _worker_cgroup_name(worker)
    parent_fd = None
    child_fd = None
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(raw.parent, flags)
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_dev != worker.get("cgroup_parent_device")
                or parent_info.st_ino != worker.get("cgroup_parent_inode")):
            raise HarnessError("parallel worker cgroup parent identity mismatch")
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        child_info = os.fstat(child_fd)
        if (child_info.st_dev != worker.get("cgroup_device")
                or child_info.st_ino != worker.get("cgroup_inode")):
            raise HarnessError("parallel worker containment stable identity mismatch")
        for control in ("cgroup.events", "cgroup.freeze", "cgroup.procs", "cgroup.kill"):
            probe = os.open(control, getattr(os, "O_PATH", os.O_RDONLY)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=child_fd)
            try:
                if not stat.S_ISREG(os.fstat(probe).st_mode):
                    raise HarnessError("worker cgroup control is not a regular control file")
            finally:
                os.close(probe)
        yield parent_fd, child_fd, name
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("cannot safely open bound worker containment") from exc
    finally:
        if child_fd is not None:
            os.close(child_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def read_cgroup_control(cgroup_fd: int, name: str) -> str:
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), dir_fd=cgroup_fd)
        with os.fdopen(fd, "r", encoding="ascii", closefd=False) as handle:
            return handle.read(64 * 1024)
    except OSError as exc:
        raise HarnessError("cannot read bound worker cgroup control") from exc
    finally:
        if fd is not None:
            os.close(fd)


def write_cgroup_control(cgroup_fd: int, name: str, value: str) -> None:
    fd = None
    try:
        fd = os.open(name, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), dir_fd=cgroup_fd)
        os.write(fd, value.encode("ascii"))
    except OSError as exc:
        raise HarnessError("cannot write bound worker cgroup control") from exc
    finally:
        if fd is not None:
            os.close(fd)


def retire_parallel_worker(state: dict[str, Any], task_id: str) -> None:
    worker = state["workers"].get(task_id)
    if not isinstance(worker, dict):
        return
    if not worker.get("activated"):
        raise HarnessError("parallel worker containment was never activated")
    with open_bound_worker_cgroup(worker) as (_, cgroup_fd, _):
        events = read_cgroup_control(cgroup_fd, "cgroup.events")
    if "populated 0" not in events:
        raise HarnessError("parallel worker containment must be empty before handoff")
    del state["workers"][task_id]


def activate_parallel_worker(state: dict[str, Any], task_id: str) -> None:
    worker = state["workers"].get(task_id)
    if not isinstance(worker, dict):
        raise HarnessError("parallel worker is not registered")
    with open_bound_worker_cgroup(worker) as (_, cgroup_fd, _):
        events = read_cgroup_control(cgroup_fd, "cgroup.events")
    if "populated 1" not in events:
        raise HarnessError("parallel worker containment is not populated")
    worker["activated"] = True


def remove_empty_parallel_containment(worker: dict[str, Any]) -> None:
    try:
        with open_bound_worker_cgroup(worker) as (parent_fd, cgroup_fd, bound_name):
            events = read_cgroup_control(cgroup_fd, "cgroup.events")
            if "populated 0" not in events:
                raise HarnessError("parallel worker containment must be empty before removal")
            os.rmdir(bound_name, dir_fd=parent_fd)
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("cannot remove empty parallel worker containment") from exc


def set_bound_worker_frozen(worker: dict[str, Any], frozen: bool) -> None:
    if not worker.get("activated"):
        raise HarnessError("parallel worker containment is not activated")
    expected = "1" if frozen else "0"
    with open_bound_worker_cgroup(worker) as (_, cgroup_fd, _):
        write_cgroup_control(cgroup_fd, "cgroup.freeze", expected)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if f"frozen {expected}" in read_cgroup_control(cgroup_fd, "cgroup.events"):
                return
            time.sleep(0.02)
    raise HarnessError("complete worker containment did not reach requested freeze state")


def apply_parallel_memory_action(action: str, workers: list[dict[str, Any]]) -> list[str]:
    if action in {"continue", "hold"}:
        return []
    if not workers:
        raise HarnessError("memory backpressure requires complete verified worker containment")
    candidates = [worker for worker in workers if worker.get("cgroup")]
    if len(candidates) != len(workers):
        raise HarnessError("memory backpressure requires complete verified worker containment")
    changed: list[str] = []
    if action in {"pause_one", "pause_heavy_first"}:
        running = [worker for worker in candidates if not worker.get("paused")]
        if not running:
            return []
        running.sort(key=lambda worker: (
            worker.get("resource_class") != "heavy" if action == "pause_heavy_first" else False,
            -int(worker.get("started_seq", 0)), str(worker.get("id", "")),
        ))
        worker = running[0]
        set_bound_worker_frozen(worker, True)
        worker["paused"] = True
        changed.append(str(worker.get("id", "")))
    elif action == "resume_one":
        paused = sorted((worker for worker in candidates if worker.get("paused")),
                        key=lambda worker: (int(worker.get("started_seq", 0)), str(worker.get("id", ""))))
        if paused:
            worker = paused[0]
            set_bound_worker_frozen(worker, False)
            worker["paused"] = False
            changed.append(str(worker.get("id", "")))
    else:
        raise HarnessError("unknown parallel memory action")
    return changed


def sample_linux_memory() -> dict[str, int | float]:
    return parse_linux_memory_metrics(
        pathlib.Path("/proc/meminfo").read_text(encoding="ascii"),
        pathlib.Path("/proc/pressure/memory").read_text(encoding="ascii"),
    )


def run_memory_guard(workers_value: dict[str, Any], *, interval: float,
                     max_samples: int | None = None, sampler=sample_linux_memory,
                     sleeper=time.sleep) -> list[dict[str, Any]]:
    if max_samples is None or max_samples < 1:
        raise HarnessError("bounded memory guard helper requires max_samples >= 1")
    workers = workers_value.get("workers")
    if not isinstance(workers, list):
        raise HarnessError("parallel workers state must contain a workers array")
    events: list[dict[str, Any]] = []
    count = 0
    while count < max_samples:
        event = memory_guard_event(workers_value, sampler=sampler)
        event["sample"] = count + 1
        events.append(event)
        count += 1
        if count < max_samples:
            sleeper(interval)
    return events


def memory_guard_event(workers_value: dict[str, Any],
                       sampler=sample_linux_memory) -> dict[str, Any]:
    workers = workers_value.get("workers")
    if not isinstance(workers, list):
        raise HarnessError("parallel workers state must contain a workers array")
    guard = workers_value.setdefault("guard", {"recovered_samples": 0, "admission_open": True})
    metrics = sampler()
    paused = sum(1 for worker in workers if worker.get("paused"))
    decision = parallel_memory_action(**metrics, paused_count=paused)
    if not workers:
        guard["admission_open"] = False
        guard["recovered_samples"] = 0
        return {**metrics, "decision": decision, "action": "serial_fallback",
                "changed_workers": [], "admission_open": False, "safe_to_continue": True}
    containment_ready = True
    for worker in workers:
        if not worker.get("activated"):
            containment_ready = False
            break
        raw = worker.get("cgroup")
        if not raw:
            containment_ready = False
            break
        try:
            cgroup = validate_worker_cgroup(pathlib.Path(raw))
            if not worker_cgroup_controls_ready(cgroup):
                containment_ready = False
                break
        except (HarnessError, OSError):
            containment_ready = False
            break
    if not containment_ready:
        guard["admission_open"] = False
        guard["recovered_samples"] = 0
        return {**metrics, "decision": decision, "action": "blocked_uncontained",
                "changed_workers": [], "admission_open": False, "safe_to_continue": False}
    guard["admission_open"] = True
    changed: list[str] = []
    action = decision
    if decision == "resume_one":
        guard["recovered_samples"] = int(guard.get("recovered_samples", 0)) + 1
        if guard["recovered_samples"] < 3:
            action = "hold_recovery"
        else:
            changed = apply_parallel_memory_action(decision, workers)
            guard["recovered_samples"] = 0
    else:
        guard["recovered_samples"] = 0
        if decision not in {"continue", "hold"}:
            changed = apply_parallel_memory_action(decision, workers)
    unresolved = decision in {"pause_one", "pause_heavy_first"}
    return {**metrics, "decision": decision, "action": action,
            "changed_workers": changed, "admission_open": not unresolved,
            "safe_to_continue": not unresolved}


def open_parallel_run_directory(root: pathlib.Path, run_id: str) -> tuple[pathlib.Path, int]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id):
        raise HarnessError("parallel run ID contains unsupported characters")
    result = run_capture(["git", "rev-parse", "--git-common-dir"], cwd=root)
    if result.returncode != 0:
        raise HarnessError("cannot resolve shared Git metadata directory")
    common = pathlib.Path(result.stdout.strip())
    if not common.is_absolute():
        common = (root / common).absolute()
    common_fd = open_directory_path(common)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            os.mkdir("harness-parallel", 0o700, dir_fd=common_fd)
        except FileExistsError:
            pass
        parent_fd = os.open("harness-parallel", flags, dir_fd=common_fd)
        try:
            try:
                os.mkdir(run_id, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            run_fd = os.open(run_id, flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise HarnessError("parallel control path contains a symlink or unsafe component") from exc
    finally:
        os.close(common_fd)
    return common / "harness-parallel" / run_id, run_fd


def parallel_control_root(root: pathlib.Path, run_id: str) -> pathlib.Path:
    control, run_fd = open_parallel_run_directory(root, run_id)
    os.close(run_fd)
    return control


def open_directory_path(path: pathlib.Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise HarnessError("parallel control ancestry contains a symlink or unsafe component") from exc


@contextlib.contextmanager
def parallel_run_lock(root: pathlib.Path, run_id: str):
    _, dir_fd = open_parallel_run_directory(root, run_id)
    handle = None
    lock_fd = None
    locked = False
    entered_body = False
    primary_error = None
    try:
        flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        lock_fd = os.open("state.lock", flags, 0o600, dir_fd=dir_fd)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_size > 1024 * 1024):
            raise HarnessError("canonical state lock must be an owner-controlled single-link regular file")
        try:
            handle = os.fdopen(lock_fd, "a+")
            lock_fd = None
        except Exception:
            raise
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        entered_body = True
        yield dir_fd
    except HarnessError as exc:
        primary_error = exc
        raise
    except OSError as exc:
        if entered_body:
            primary_error = exc
            raise
        primary_error = HarnessError("cannot safely open canonical parallel state lock")
        raise primary_error from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error = None
        if handle is not None:
            import fcntl
            if locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    cleanup_error = exc
            try:
                handle.close()
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        try:
            os.close(dir_fd)
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None and primary_error is None:
            raise HarnessError("cannot safely release canonical parallel state lock") from cleanup_error


def read_parallel_bundle(dir_fd: int) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open("run.json", flags, dir_fd=dir_fd)
    except OSError as exc:
        raise HarnessError("cannot safely read canonical parallel run") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_size > 2 * 1024 * 1024):
            raise HarnessError("canonical parallel run must be an owner-controlled single-link regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("cannot safely read canonical parallel run") from exc
    finally:
        os.close(fd)
    if len(raw) > 2 * 1024 * 1024:
        raise HarnessError("canonical parallel run is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError("canonical parallel run is invalid JSON") from exc
    if not isinstance(value, dict):
        raise HarnessError("canonical parallel run must be an object")
    return value


def write_parallel_bundle(dir_fd: int, bundle: dict[str, Any], *, create_only: bool = False) -> None:
    data = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temp = f".run.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    published = False
    publication = "unpublished"
    primary_error = None
    try:
        fd = os.open(temp, flags, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        if create_only:
            try:
                publication = "ambiguous"
                os.link(temp, "run.json", src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                        follow_symlinks=False)
            except FileExistsError as exc:
                raise HarnessError("parallel run ID is already frozen")
            published = True
            publication = "published"
            os.unlink(temp, dir_fd=dir_fd)
        else:
            existing = os.open("run.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                               | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
            try:
                info = os.fstat(existing)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_size > 2 * 1024 * 1024):
                    raise HarnessError("existing canonical run target is unsafe")
            finally:
                os.close(existing)
            publication = "ambiguous"
            os.replace(temp, "run.json", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            published = True
            publication = "published"
        os.fsync(dir_fd)
    except HarnessError as exc:
        primary_error = exc
        setattr(exc, "canonical_publication", publication)
        setattr(exc, "canonical_published", publication != "unpublished")
        raise
    except OSError as exc:
        primary_error = HarnessError("cannot safely publish canonical parallel run")
        setattr(primary_error, "canonical_publication", publication)
        setattr(primary_error, "canonical_published", publication != "unpublished")
        raise primary_error from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError as exc:
                if primary_error is None:
                    close_error = HarnessError("cannot close canonical parallel temporary file")
                    setattr(close_error, "canonical_publication", publication)
                    setattr(close_error, "canonical_published", publication != "unpublished")
                    primary_error = close_error
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if primary_error is None:
                cleanup_error = HarnessError("cannot clean canonical parallel temporary file")
                setattr(cleanup_error, "canonical_publication", publication)
                setattr(cleanup_error, "canonical_published", publication != "unpublished")
                raise cleanup_error from exc
        if primary_error is not None:
            import sys
            if sys.exc_info()[0] is None:
                raise primary_error


def freeze_parallel_plan_command(args: argparse.Namespace) -> None:
    root = git_root(True)
    plan_path = pathlib.Path(args.plan_file)
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    plan = load_json(plan_path)
    schema = skill_root() / "references/contracts/parallel-plan.schema.json"
    freeze = freeze_parallel_plan(root, plan, schema, args.run_id)
    with parallel_run_lock(root, args.run_id) as dir_fd:
        write_parallel_bundle(dir_fd, parallel_bundle(plan, freeze), create_only=True)
    emit("freeze-parallel-plan", "ok", run_id=args.run_id,
         contract_set_id=freeze["contract_set_id"])


def validate_parallel_lane_command(args: argparse.Namespace) -> None:
    root = git_root(True)
    report = validate_parallel_lane(root, args.base_sha, args.head_sha, args.write_path)
    if args.json:
        print(json.dumps(report, separators=(",", ":")))
    else:
        emit("validate-parallel-lane", "ok", **report)


def memory_guard_step_command(args: argparse.Namespace) -> None:
    metrics = sample_linux_memory()
    workers_path = pathlib.Path(args.workers_file)
    workers_value = load_json(workers_path)
    if not isinstance(workers_value, dict) or not isinstance(workers_value.get("workers"), list):
        raise HarnessError("parallel workers file must contain a workers array")
    workers = workers_value["workers"]
    paused_count = sum(1 for worker in workers if worker.get("paused"))
    action = parallel_memory_action(**metrics, paused_count=paused_count)
    if args.apply:
        raise HarnessError("memory guard apply requires canonical --run-id authority")
    result = {**metrics, "action": action, "changed_workers": [],
              "applied": False, "paused_count": paused_count}
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        emit("memory-guard-step", "ok", **result)


def record_parallel_lane_command(args: argparse.Namespace) -> None:
    root = git_root(True)
    with parallel_run_lock(root, args.run_id) as dir_fd:
        bundle = read_parallel_bundle(dir_fd)
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        plan = bundle["plan"]
        state = bundle["state"]
        task = next((item for item in plan["tasks"] if item["id"] == args.task_id), None)
        if task is None:
            raise HarnessError("unknown parallel task")
        report = validate_parallel_lane(root, state["base_sha"], args.head_sha, task["write_paths"])
        worker = dict(state["workers"].get(args.task_id, {}))
        record_parallel_lane(state, plan, args.task_id, args.head_sha, report["changed_paths"])
        retire_parallel_worker(state, args.task_id)
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        write_parallel_bundle(dir_fd, bundle)
        if worker:
            try:
                remove_empty_parallel_containment(worker)
            except HarnessError:
                pass
    emit("record-parallel-lane", "ok", run_id=args.run_id, task_id=args.task_id,
         head_sha=args.head_sha)


def parallel_integration_state_command(args: argparse.Namespace) -> None:
    root = git_root(True)
    with parallel_run_lock(root, args.run_id) as dir_fd:
        bundle = read_parallel_bundle(dir_fd)
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        result = parallel_integration_state(bundle["state"])
    print(json.dumps(result, separators=(",", ":")))


def register_parallel_worker_command(args: argparse.Namespace) -> None:
    root = git_root(True)
    with parallel_run_lock(root, args.run_id) as dir_fd:
        bundle = read_parallel_bundle(dir_fd)
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        state = bundle["state"]
        if not parallel_task_ready(state, bundle["plan"], args.task_id):
            raise HarnessError("worker task dependencies are not ready")
        if args.task_id in state["workers"]:
            raise HarnessError("parallel worker is already registered")
        containment = create_parallel_worker_containment(args.run_id, args.task_id,
                                                          state["generation"])
        worker = {
            "id": args.task_id, "task_id": args.task_id, "run_id": args.run_id,
            "generation": state["generation"], **containment, "activated": False,
            "paused": False,
            "resource_class": next(task["resource_class"] for task in bundle["plan"]["tasks"]
                                   if task["id"] == args.task_id),
        }
        state["workers"][args.task_id] = worker
        try:
            validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
            write_parallel_bundle(dir_fd, bundle)
        except Exception as exc:
            state["workers"].pop(args.task_id, None)
            if getattr(exc, "canonical_publication", None) == "unpublished":
                try:
                    remove_empty_parallel_containment(worker)
                except HarnessError:
                    pass
            raise
    emit("register-parallel-worker", "ok", run_id=args.run_id, task_id=args.task_id,
         generation=state["generation"], cgroup=containment["cgroup"])


def activate_parallel_worker_command(args: argparse.Namespace) -> None:
    root = git_root(True)
    with parallel_run_lock(root, args.run_id) as dir_fd:
        bundle = read_parallel_bundle(dir_fd)
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        activate_parallel_worker(bundle["state"], args.task_id)
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        write_parallel_bundle(dir_fd, bundle)
    emit("activate-parallel-worker", "ok", run_id=args.run_id, task_id=args.task_id)


def canonical_guard_event(root: pathlib.Path, run_id: str, recovered_samples: int) -> tuple[dict[str, Any], int]:
    with parallel_run_lock(root, run_id) as dir_fd:
        bundle = read_parallel_bundle(dir_fd)
        validate_parallel_bundle(root, bundle,
                                 skill_root() / "references/contracts/parallel-plan.schema.json",
                                 reconcile_paused=True)
        state = bundle["state"]
        workers = list(state["workers"].values())
        guard_value = {"workers": workers,
                       "guard": {"recovered_samples": recovered_samples,
                                 "admission_open": True}}
        event = memory_guard_event(guard_value)
        next_recovered = int(guard_value["guard"]["recovered_samples"])
        state["workers"] = {worker["task_id"]: worker for worker in workers}
        validate_parallel_bundle(root, bundle, skill_root() / "references/contracts/parallel-plan.schema.json")
        write_parallel_bundle(dir_fd, bundle)
        return event, next_recovered


def memory_guard_command(args: argparse.Namespace) -> None:
    if args.interval <= 0 or (args.max_samples is not None and args.max_samples < 1):
        raise HarnessError("memory guard interval and max samples must be positive")
    root = git_root(True)
    count = 0
    recovered_samples = 0
    while args.max_samples is None or count < args.max_samples:
        event, recovered_samples = canonical_guard_event(root, args.run_id, recovered_samples)
        event["sample"] = count + 1
        print(json.dumps(event, separators=(",", ":")))
        sys.stdout.flush()
        count += 1
        if args.max_samples is None or count < args.max_samples:
            time.sleep(args.interval)


def add_parallel_parsers(sub: argparse._SubParsersAction) -> None:
    sp = sub.add_parser("freeze-parallel-plan", help="freeze a clean committed parallel plan")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--plan-file", required=True)
    sp.set_defaults(func=freeze_parallel_plan_command)
    sp = sub.add_parser("validate-parallel-lane", help="validate Git-derived lane scope and ancestry")
    sp.add_argument("--base-sha", required=True)
    sp.add_argument("--head-sha", required=True)
    sp.add_argument("--write-path", action="append", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=validate_parallel_lane_command)
    sp = sub.add_parser("record-parallel-lane", help="record one Git-validated lane")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--task-id", required=True)
    sp.add_argument("--head-sha", required=True)
    sp.set_defaults(func=record_parallel_lane_command)
    sp = sub.add_parser("parallel-integration-state", help="show integration readiness")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=parallel_integration_state_command)
    sp = sub.add_parser("register-parallel-worker", help="bind a worker cgroup to the canonical run")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--task-id", required=True)
    sp.set_defaults(func=register_parallel_worker_command)
    sp = sub.add_parser("activate-parallel-worker", help="prove a registered worker cgroup is populated")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--task-id", required=True)
    sp.set_defaults(func=activate_parallel_worker_command)
    sp = sub.add_parser("memory-guard-step", help="sample and optionally apply cgroup memory backpressure")
    sp.add_argument("--workers-file", required=True)
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=memory_guard_step_command)
    sp = sub.add_parser("memory-guard", help="continuously apply memory backpressure")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--max-samples", type=int)
    sp.set_defaults(func=memory_guard_command)
