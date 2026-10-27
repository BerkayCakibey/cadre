"""Operator permission checks: typed approvals, model allow-lists, stopped-plan guards."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sys
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterator
import uuid

from kosu import FileLock, RunStore, atomic_json, health_json, plan_hold


POLICY: dict[str, Any] = {
    "version": 1,
    "approvals": {
        "commit": 2,
        "push": 2,
        "merge": 2,
        "transfer": 2,
        "install": 1,
        "cleanup": 1,
        "restore": 0,
        "init-commit": 2,
        "publish": 2,
        "cloud-write": 2,
        "model-calls": 2,
    },
    "cloud": "read-only",
    "secrets": "deny",
    "local-edit": "allow",
    "claude-quota": {"five_hour": 85, "seven_day": 90},
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def scope(store: RunStore, run_id: str) -> Path:
    return store.home / "operator-system" if run_id == "system" else store.path(run_id)


def scope_record(store: RunStore, run_id: str) -> dict[str, Any]:
    return (
        {"state": "system", "plan_history": []}
        if run_id == "system"
        else store.read(run_id)
    )


def policy(store: RunStore, run_id: str) -> dict[str, Any]:
    path = scope(store, run_id) / "operator-policy.json"
    if not path.exists() and not path.is_symlink():
        atomic_json(path, POLICY)
    current = health_json(store, path)
    if current != POLICY:
        raise PermissionError("operator-policy-invalid")
    return current


def identity(
    store: RunStore,
    run_id: str,
    kind: str,
    commands: list[list[str]],
    target: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    current = policy(store, run_id)
    if (
        kind not in current["approvals"]
        or not commands
        or len(commands) > 32
        or any(
            not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            for command in commands
        )
    ):
        raise ValueError("operator-action-invalid")
    value = {
        "run_id": run_id,
        "kind": kind,
        "commands": commands,
        "target": target,
        "product": product,
        "policy": digest(current),
        "plan_revision": 0
        if run_id == "system"
        else store.plan_view(run_id)["revision"],
    }
    if len(json.dumps(value).encode()) > 100_000:
        raise ValueError("operator-action-too-large")
    return value


def propose(
    store: RunStore,
    run_id: str,
    kind: str,
    commands: list[list[str]],
    target: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    from kosu_defteri import redakte

    serialized = json.dumps(commands, ensure_ascii=False)
    if redakte(serialized) != serialized:
        raise PermissionError("operator-secret-argument-denied")
    value = identity(store, run_id, kind, commands, target, product)
    folder = scope(store, run_id) / "operator-actions"
    with FileLock(scope(store, run_id) / ".operator-actions.lock"):
        matches = []
        for path in folder.glob("*.json"):
            action = read_action(store, run_id, path.stem)
            if {
                key: item
                for key, item in action["identity"].items()
                if key != "generation"
            } == value:
                matches.append(action)
        latest = (
            max(matches, key=lambda action: action["identity"]["generation"])
            if matches
            else None
        )
        if (
            latest
            and latest["status"] not in ("stale", "failed")
            and not (kind == "push" and latest["status"] == "uncertain")
            and not (kind == "install" and latest["status"] == "completed")
        ):
            return latest
        value["generation"] = latest["identity"]["generation"] + 1 if latest else 1
        token = digest(value)
        path = folder / (token + ".json")
        action = {
            "token": token,
            "identity": value,
            "required": POLICY["approvals"][kind],
            "status": "approved" if POLICY["approvals"][kind] == 0 else "pending",
            "messages": [],
            "created": time.time(),
            "display": "\n".join(shlex.join(command) for command in commands),
        }
        atomic_json(path, action)
        return action


def read_action(store: RunStore, run_id: str, token: str) -> dict[str, Any]:
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("operator-action-token-invalid")
    action = health_json(
        store, scope(store, run_id) / "operator-actions" / (token + ".json")
    )
    identity = action.get("identity", {})
    if (
        action.get("token") != token
        or digest(identity) != token
        or identity.get("run_id") != run_id
        or identity.get("policy") != digest(policy(store, run_id))
        or action.get("required") != POLICY["approvals"].get(identity.get("kind"))
    ):
        raise PermissionError("operator-action-untrusted")
    return action


def approve(
    store: RunStore, run_id: str, token: str, confirmation: str = ""
) -> dict[str, Any]:
    if os.environ.get("CADRE_RUN_ID"):
        raise PermissionError("operator-approval-command-required")
    with FileLock(scope(store, run_id) / ".operator-actions.lock"):
        action = read_action(store, run_id, token)
        record = scope_record(store, run_id)
        if plan_hold(record) or (scope(store, run_id) / "cancel.flag").exists():
            raise PermissionError("operator-action-stopped")
        if action["status"] == "pending" and not confirmation:
            action["messages"].append(
                {"id": uuid.uuid4().hex, "stage": 1, "at": time.time()}
            )
            action["status"] = (
                "approved" if action["required"] == 1 else "first-approved"
            )
            action["confirmation"] = uuid.uuid4().hex
        elif (
            action["status"] == "first-approved"
            and confirmation
            and confirmation == action.get("confirmation")
            and len(action["messages"]) == 1
        ):
            action["messages"].append(
                {"id": uuid.uuid4().hex, "stage": 2, "at": time.time()}
            )
            action["status"] = "approved"
        else:
            raise PermissionError("operator-next-approval-required")
        atomic_json(
            scope(store, run_id) / "operator-actions" / (token + ".json"), action
        )
    if run_id != "system":
        store.mudahale(
            run_id,
            "approve",
            {"kind": action["identity"]["kind"], "stage": len(action["messages"])},
        )
    return action


@contextmanager
def permitted(
    store: RunStore, run_id: str, token: str, current: Callable[[], dict[str, Any]]
) -> Iterator[dict[str, Any]]:
    with FileLock(scope(store, run_id) / ".operator-actions.lock"):
        action = read_action(store, run_id, token)
        if (
            plan_hold(scope_record(store, run_id))
            or (scope(store, run_id) / "cancel.flag").exists()
        ):
            raise PermissionError("operator-action-stopped")
        if (
            action["status"] != "approved"
            or len(action["messages"]) != action["required"]
            or len({message["id"] for message in action["messages"]})
            != action["required"]
        ):
            raise PermissionError("operator-approval-required")
        try:
            expected = current()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            action.update(status="stale")
            atomic_json(
                scope(store, run_id) / "operator-actions" / (token + ".json"), action
            )
            if str(error).startswith("operator-"):
                raise
            raise PermissionError("operator-action-stale") from None
        expected.setdefault("generation", action["identity"]["generation"])
        path = scope(store, run_id) / "operator-actions" / (token + ".json")
        if action["identity"] != expected:
            action.update(status="stale")
            atomic_json(path, action)
            raise PermissionError("operator-action-stale")
        action.update(status="in-flight", consumed=time.time())
        atomic_json(path, action)
        try:
            yield action
        except BaseException as error:
            action.update(status="uncertain", error=type(error).__name__)
            atomic_json(path, action)
            raise
        else:
            action.update(status="completed", finished=time.time())
            atomic_json(path, action)


def pending(store: RunStore, run_id: str) -> list[dict[str, Any]]:
    folder = scope(store, run_id) / "operator-actions"
    return [
        action
        for path in sorted(folder.glob("*.json"))
        if (action := read_action(store, run_id, path.stem))["status"]
        not in ("completed", "superseded")
    ]


def no_effect(
    store: RunStore, run_id: str, token: str, current: Callable[[], dict[str, Any]]
) -> None:
    with FileLock(scope(store, run_id) / ".operator-actions.lock"):
        action = read_action(store, run_id, token)
        value = current()
        value.setdefault("generation", action["identity"]["generation"])
        if action["status"] != "uncertain" or action["identity"] != value:
            raise PermissionError("operator-effect-unresolved")
        action["status"] = "failed"
        atomic_json(
            scope(store, run_id) / "operator-actions" / (token + ".json"), action
        )


def resolve(store: RunStore, run_id: str, token: str, outcome: str) -> dict[str, Any]:
    """FIX-31: an action left in-flight or uncertain after SIGKILL/timeout is closed explicitly by the operator; denied if its owner is alive."""
    from kosu import process_identity

    if outcome not in ("etkisiz", "tamamlandi"):
        raise ValueError("operator-resolution-invalid")
    owner = FileLock(scope(store, run_id) / ".owner.lock")
    if not owner.acquire(blocking=False):
        raise PermissionError("operator-owner-active")
    try:
        with FileLock(scope(store, run_id) / ".operator-actions.lock"):
            action = read_action(store, run_id, token)
            if action["status"] not in ("in-flight", "uncertain"):
                raise PermissionError("operator-effect-unresolved")
            observer = (action.get("effect_seed") or {}).get("process")
            if (
                action["status"] == "in-flight"
                and observer
                and process_identity(observer["pid"]) == observer
            ):
                raise PermissionError("operator-effect-owner-active")
            action.update(
                status="failed" if outcome == "etkisiz" else "completed",
                finished=time.time(),
                resolution={"outcome": outcome, "previous": action["status"], "by": "operator", "ts": time.time()},
            )
            atomic_json(scope(store, run_id) / "operator-actions" / (token + ".json"), action)
    finally:
        owner.close()
    if run_id != "system":
        store.mudahale(run_id, "operator-resolve", {"token": token[:12], "kind": action["identity"]["kind"], "outcome": outcome})
    return action


def git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def git_target(path: Path) -> dict[str, Any]:
    common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    worktree_config = Path(git(path, "rev-parse", "--git-path", "config.worktree"))
    if not worktree_config.is_absolute():
        worktree_config = path / worktree_config
    configs = []
    for config in (common / "config", worktree_config):
        if config.is_file():
            info = config.stat()
            configs.append(
                [str(config), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
            )
    return {
        "path": str(path.resolve()),
        "branch": git(path, "branch", "--show-current"),
        "head": git(path, "rev-parse", "HEAD"),
        "config_identity": configs,
    }


def safe_hooks(path: Path) -> None:
    filters = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "config",
            "--name-only",
            "--get-regexp",
            r"^filter[.].*[.](clean|smudge|process)$",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if filters.returncode != 1:
        raise PermissionError("operator-git-filter-untrusted")
    configured = subprocess.run(
        ["git", "-C", str(path), "config", "--path", "core.hooksPath"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    hooks = (
        Path(configured.stdout.strip())
        if configured.returncode == 0
        else Path(git(path, "rev-parse", "--git-path", "hooks"))
    )
    if not hooks.is_absolute():
        hooks = path / hooks
    trusted = Path(__file__).resolve().parents[1] / "hooks"
    if hooks.is_dir():
        for hook in hooks.iterdir():
            if (
                not hook.is_file()
                or hook.name.endswith(".sample")
                or not os.access(hook, os.X_OK)
            ):
                continue
            expected = trusted / hook.name
            if (
                hook.is_symlink()
                or hook.stat().st_nlink != 1
                or not expected.is_file()
                or hashlib.sha256(hook.read_bytes()).digest()
                != hashlib.sha256(expected.read_bytes()).digest()
            ):
                raise PermissionError("operator-git-hooks-untrusted")
    configured = subprocess.run(
        ["git", "-C", str(path), "config", "--local", "--path", "core.hooksPath"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    original = (
        Path(configured.stdout.strip())
        if configured.returncode == 0
        else Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "hooks"
    )
    if not original.is_absolute():
        original = path / original
    if original.resolve() != hooks.resolve() and original.is_dir():
        for hook in original.iterdir():
            if (
                hook.is_file()
                and not hook.name.endswith(".sample")
                and os.access(hook, os.X_OK)
            ):
                raise PermissionError("operator-git-hooks-untrusted")


def commit_details(
    store: RunStore, run_id: str, wt: Path, message: str
) -> tuple[list[list[str]], dict[str, Any], dict[str, Any]]:
    import qe_core as core
    from sonuc import revision

    record = scope_record(store, run_id)
    wt = wt.resolve(strict=True)
    if (
        not wt.is_relative_to((store.home / "worktrees" / record["project"]).resolve())
        or Path(record.get("worktree") or "").resolve() != wt
        or not git(wt, "branch", "--show-current").startswith("cadre/")
    ):
        raise PermissionError("operator-commit-worktree-invalid")
    if not message or len(message) > 200 or "\n" in message or "\0" in message:
        raise ValueError("operator-commit-message-invalid")
    safe_hooks(wt)
    product = {"revision": revision(wt), "tree": core.agac_sha(str(wt))}
    commands = [
        ["git", "-c", "core.fsmonitor=false", "-C", str(wt), "add", "-A", "--", "."]
    ]
    paths = [
        pattern.replace(":(exclude,glob)", ":(glob)").replace(":(exclude)", "")
        for pattern in core.URUN_HARIC
    ]
    commands.append(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(wt),
            "rm",
            "--cached",
            "-r",
            "-q",
            "--ignore-unmatch",
            "--",
            *paths,
        ]
    )
    commands.append(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(wt),
            "commit",
            "-q",
            "-m",
            message,
        ]
    )
    return commands, git_target(wt), product


def commit_proposal(
    store: RunStore, run_id: str, wt: Path, message: str
) -> dict[str, Any]:
    return propose(store, run_id, "commit", *commit_details(store, run_id, wt, message))


def commit_execute(store: RunStore, run_id: str, token: str) -> dict[str, Any]:
    action = read_action(store, run_id, token)
    owner = FileLock(scope(store, run_id) / ".owner.lock")
    if not owner.acquire(blocking=False):
        raise PermissionError("operator-owner-active")
    try:
        with FileLock(
            store.home
            / "locks"
            / (
                "operator-target-"
                + digest(action["identity"]["target"]["path"])
                + ".lock"
            )
        ):
            return _commit_execute(store, run_id, token)
    finally:
        owner.close()


def _commit_execute(store: RunStore, run_id: str, token: str) -> dict[str, Any]:
    import qe_core as core
    from sonuc import revision

    action = read_action(store, run_id, token)
    if action["identity"]["kind"] != "commit":
        raise ValueError("operator-action-kind-invalid")
    wt = Path(action["identity"]["target"]["path"])
    message = action["identity"]["commands"][-1][-1]

    def current() -> dict[str, Any]:
        return identity(
            store, run_id, "commit", *commit_details(store, run_id, wt, message)
        )

    with permitted(store, run_id, token, current) as receipt:
        files = len(core.urun_kirli(str(wt)).splitlines())
        index_path = Path(git(wt, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = wt / index_path
        lock = index_path.with_name(index_path.name + ".lock")
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        index = None
        try:
            index_fd, index_name = tempfile.mkstemp(
                prefix="operator-commit-index-", dir=index_path.parent
            )
            os.close(index_fd)
            index = Path(index_name)
            if index_path.exists():
                shutil.copyfile(index_path, index)
            else:
                index.unlink()
            env = {
                **os.environ,
                "GIT_INDEX_FILE": str(index),
                "GIT_REFLOG_ACTION": "cadre-operator-" + token,
            }
            for command in action["identity"]["commands"][:-1]:
                subprocess.run(
                    command, env=env, capture_output=True, check=True, timeout=60
                )
            if current() != {
                key: value
                for key, value in action["identity"].items()
                if key != "generation"
            }:
                raise PermissionError("operator-action-stale")
            from kosu import process_identity

            info = os.fstat(fd)
            receipt["effect_seed"] = {
                "process": process_identity(os.getpid()),
                "index": str(index_path),
                "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
                "lock_identity": [info.st_dev, info.st_ino],
                "files": files,
            }
            atomic_json(
                scope(store, run_id) / "operator-actions" / (token + ".json"), receipt
            )
            subprocess.run(
                action["identity"]["commands"][-1],
                env=env,
                capture_output=True,
                check=True,
                timeout=60,
            )
            os.replace(index, index_path)
            if core.agac_sha(str(wt)) != action["identity"]["product"]["tree"]:
                raise PermissionError("operator-committed-product-changed")
            receipt["result"] = {
                "revision": revision(wt),
                "tree": action["identity"]["product"]["tree"],
                "files": files,
            }
        finally:
            before, after = os.fstat(fd), lock.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(fd)
                raise PermissionError("operator-index-lock-changed")
            os.close(fd)
            lock.unlink()
            if index is not None and index.exists():
                index.unlink()
        return receipt["result"]


def delivery_candidate(
    record: dict[str, Any], acceptance: Callable[[dict[str, Any]], bool] | None = None
) -> bool:
    from sonuc import approved, decision

    if record.get("state") != "completed" or not record.get("worktree"):
        return False
    state = record.get("delivery_state") or {}
    if state.get("canonical_push") == "completed":
        return False
    if Path(record["worktree"]).is_dir():
        return (acceptance or approved)(record) and (
            "teslim_merge" not in record or bool(state.get("merge_sha"))
        )
    cleanup = record.get("cleanup") or {}
    result = record.get("final_result") or {}
    try:
        manifest = Path(cleanup["worktree_archive"])
        home = Path(record["prompt"]).parent.parent.parent
        if (
            manifest.is_symlink()
            or manifest.resolve().parent
            != (home / "archives" / record["run_id"] / "worktree").resolve()
        ):
            return False
        data = json.loads(manifest.read_text())
        from arsiv import fingerprint

        return (
            data.get("version") == 1
            and fingerprint(data["entries"]) == data.get("fingerprint")
            and result.get("status") == "PASS"
            and decision(result["qa"], result["review"], result["revision"])["status"]
            == "PASS"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def obtain(store: RunStore, run_id: str, action: dict[str, Any]) -> str:
    if os.environ.get("CADRE_RUN_ID"):
        raise PermissionError("operator-approval-command-required")
    action = read_action(store, run_id, action["token"])

    def answer(question: str) -> bool:
        try:
            return input(question).strip().lower() in ("evet", "e", "yes")
        except EOFError:
            return False

    if action["status"] == "pending":
        print(
            "Eylem: "
            + action["identity"]["kind"]
            + " · "
            + json.dumps(action["identity"]["target"], ensure_ascii=False)
        )
    print("Çalışacak komut: " + action["display"], flush=True)
    if action["status"] == "pending":
        if not answer("Bu eylemi onaylıyor musun? (evet/hayır): "):
            raise PermissionError("operator-declined")
        action = approve(store, run_id, action["token"])
    if action["status"] == "first-approved":
        if not answer("Bu komut için ikinci onay (evet/hayır): "):
            raise PermissionError("operator-declined")
        action = approve(store, run_id, action["token"], action["confirmation"])
    if action["status"] != "approved":
        raise PermissionError("operator-approval-required")
    return action["token"]


def adopt_commit(store: RunStore, run_id: str, action: dict[str, Any]) -> None:
    from sonuc import approved, revision
    import qe_core as core

    result = action.get("result") or {}
    with FileLock(scope(store, run_id) / ".record.lock"):
        record = scope_record(store, run_id)
        previous = record.get("final_result") or {}
        if (previous.get("delivery") or {}).get("action_token") != action["token"]:
            raise PermissionError("operator-commit-acceptance-mismatch")
        wt = Path(record["worktree"])
        if result.get("revision") != revision(wt) or result.get(
            "tree"
        ) != core.agac_sha(str(wt)):
            raise PermissionError("operator-commit-acceptance-mismatch")
        anchored = {
            **previous,
            "revision": result["revision"],
            "qa": {**previous["qa"], "revision": result["revision"]},
            "review": {**previous["review"], "revision": result["revision"]},
            "delivery": {
                **previous["delivery"],
                "revision": result["revision"],
                "git_permission": "operator-committed",
            },
            "commit_adoption": {
                "token": action["token"],
                "tree": result["tree"],
                "previous": digest(previous),
            },
        }
        candidate = {**record, "final_result": anchored}
        if not approved(candidate):
            raise PermissionError("operator-commit-acceptance-mismatch")
        atomic_json(scope(store, run_id) / "run.json", candidate)


def record_delivery(store: RunStore, run_id: str, action: dict[str, Any]) -> None:
    result = action.get("result") or {}
    operation = result["operation"]
    with FileLock(scope(store, run_id) / ".record.lock"):
        record = scope_record(store, run_id)
        state = record.get("delivery_state") or {}
        if operation == "merge":
            state.update(
                merge_sha=result["sha"],
                source_sha=result["source_sha"],
                main_path=result["path"],
                main_branch=result["branch"],
                canonical_push="pending",
            )
            record["teslim_merge"] = time.strftime("%F %T")
        elif operation == "canonical-push":
            state.update(canonical_push="completed")
            record["teslim_merge_push"] = time.strftime("%F %T")
        elif operation == "push":
            record["teslim_push"] = time.strftime("%F %T")
        else:
            raise ValueError("operator-delivery-operation-invalid")
        state[operation + "_token"] = action["token"]
        record["delivery_state"] = state
        atomic_json(scope(store, run_id) / "run.json", record)


def reconcile_effect(store: RunStore, run_id: str, token: str) -> None:
    import qe_core as core
    from kosu import process_identity
    from sonuc import revision

    owner = FileLock(scope(store, run_id) / ".owner.lock")
    if not owner.acquire(blocking=False):
        raise PermissionError("operator-owner-active")
    try:
        action = read_action(store, run_id, token)
        target = action["identity"]["target"]
        with FileLock(
            store.home
            / "locks"
            / ("operator-target-" + digest(target["path"]) + ".lock")
        ):
            with FileLock(scope(store, run_id) / ".operator-actions.lock"):
                action = read_action(store, run_id, token)
                if action["status"] not in ("in-flight", "uncertain"):
                    return
                if (
                    len(action["messages"]) != action["required"]
                    or len({message["id"] for message in action["messages"]})
                    != action["required"]
                ):
                    raise PermissionError("operator-effect-unresolved")
                seed = action.get("effect_seed") or {}
                observer = seed.get("process")
                if (
                    action["status"] == "in-flight"
                    and observer
                    and process_identity(observer["pid"]) == observer
                ):
                    raise PermissionError("operator-effect-owner-active")
                kind = action["identity"]["kind"]
                if kind not in ("commit", "merge"):
                    raise PermissionError("operator-effect-unresolved")
                path = Path(target["path"])
                safe_hooks(path)
                head = git(path, "rev-parse", "HEAD")
                current_target = git_target(path)
                if any(
                    current_target[key] != target[key]
                    for key in ("path", "branch", "config_identity")
                ):
                    raise PermissionError("operator-effect-target-changed")
                parents = git(path, "rev-list", "--parents", "-n", "1", head).split()
                expected = [head, target["head"]] + (
                    [target["source_sha"]] if kind == "merge" else []
                )
                reflog = git(path, "reflog", "-1", "--format=%gs")
                if parents != expected or not reflog.startswith(
                    "cadre-operator-" + token + ":"
                ):
                    raise PermissionError("operator-effect-unresolved")
                if kind == "commit":
                    if (
                        not seed
                        or core.agac_sha(str(path))
                        != action["identity"]["product"]["tree"]
                    ):
                        raise PermissionError("operator-effect-product-changed")
                    index_path = Path(seed["index"])
                    lock = index_path.with_name(index_path.name + ".lock")
                    if lock.exists():
                        info = lock.lstat()
                        if (
                            lock.is_symlink()
                            or [info.st_dev, info.st_ino] != seed["lock_identity"]
                        ):
                            raise PermissionError("operator-index-lock-changed")
                        lock.unlink()
                    if git(path, "diff", "--cached", "--name-only", "HEAD"):
                        if (
                            hashlib.sha256(index_path.read_bytes()).hexdigest()
                            != seed["index_sha256"]
                        ):
                            raise PermissionError("operator-index-changed")
                        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        os.close(fd)
                        temporary = index_path.with_name("recovered-index-" + token)
                        try:
                            git(
                                path,
                                "read-tree",
                                head,
                                env={**os.environ, "GIT_INDEX_FILE": str(temporary)},
                            )
                            os.replace(temporary, index_path)
                        finally:
                            if temporary.exists():
                                temporary.unlink()
                            lock.unlink()
                    action["result"] = {
                        "revision": revision(path),
                        "tree": action["identity"]["product"]["tree"],
                        "files": seed["files"],
                    }
                else:
                    if git(path, "status", "--porcelain"):
                        raise PermissionError("operator-effect-product-changed")
                    action["result"] = {
                        "operation": "merge",
                        "sha": head,
                        "path": str(path),
                        "branch": target["ref"],
                        "source_sha": target["source_sha"],
                    }
                action.update(status="completed", finished=time.time(), reconciled=True)
                atomic_json(
                    scope(store, run_id) / "operator-actions" / (token + ".json"),
                    action,
                )
    finally:
        owner.close()


def ancestor(path: Path, sha: str) -> bool:
    """FIX-13: a merge with a missing record is taken over if it is an ancestor of HEAD, even if the main branch moved on."""
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(path), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


def recover_delivery(store: RunStore, run_id: str) -> None:
    for path in sorted((scope(store, run_id) / "operator-actions").glob("*.json")):
        action = read_action(store, run_id, path.stem)
        if action["status"] in ("in-flight", "uncertain") and action["identity"][
            "kind"
        ] in ("commit", "merge"):
            reconcile_effect(store, run_id, action["token"])
    record = scope_record(store, run_id)
    token = (record.get("final_result", {}).get("delivery") or {}).get("action_token")
    if token:
        action = read_action(store, run_id, token)
        if action["status"] == "completed" and (
            record["final_result"]["delivery"].get("git_permission") == "required"
        ):
            adopt_commit(store, run_id, action)
    for path in sorted((scope(store, run_id) / "operator-actions").glob("*.json")):
        action = read_action(store, run_id, path.stem)
        result = action.get("result") or {}
        operation = result.get("operation")
        if action["status"] == "completed" and operation in (
            "merge",
            "canonical-push",
            "push",
        ):
            current = scope_record(store, run_id).get("delivery_state") or {}
            if current.get(operation + "_token") != action["token"]:
                target = Path(result["path"])
                if operation == "merge" and not ancestor(target, result["sha"]):
                    note = {"status": "unrecorded", "reason": "operator-delivery-recovery-mismatch",
                            "head": git(target, "rev-parse", "HEAD")}
                    if action.get("recovery") != note:
                        action["recovery"] = note
                        atomic_json(path, action)
                    continue
                record_delivery(store, run_id, action)


def delivery_details(
    store: RunStore, run_id: str, operation: str
) -> tuple[str, list[list[str]], dict[str, Any], dict[str, Any]]:
    from sonuc import approved, revision

    record = scope_record(store, run_id)
    if record["state"] != "completed" or not approved(record) or plan_hold(record):
        raise PermissionError("operator-reviewed-product-required")
    wt = Path(record["worktree"]).resolve(strict=True)
    branch = git(wt, "branch", "--show-current")
    if (
        not branch.startswith("cadre/")
        or (record.get("final_result", {}).get("delivery") or {}).get("git_permission")
        == "required"
    ):
        raise PermissionError("operator-commit-required")
    source_sha = git(wt, "rev-parse", "HEAD")
    state = record.get("delivery_state") or {}
    if operation == "push":
        path, ref, sha, kind = wt, branch, source_sha, "push"
        commands = [
            ["git", "-C", str(path), "push", "origin", sha + ":refs/heads/" + ref]
        ]
    elif operation in ("merge", "canonical-push"):
        project = store.home / "projects" / record["project"]
        path = Path((project / "repo-path").read_text().strip()).resolve(strict=True)
        ref = (project / "base-branch").read_text().strip()
        if git(path, "branch", "--show-current") != ref:
            raise PermissionError("operator-canonical-branch-changed")
        if git(path, "status", "--porcelain"):
            raise PermissionError("operator-canonical-dirty")
        if operation == "canonical-push":
            sha, kind = state["merge_sha"], "push"
            if (
                state.get("source_sha") != source_sha
                or state.get("main_path") != str(path)
                or state.get("main_branch") != ref
                or git(path, "rev-parse", "HEAD") != sha
            ):
                raise PermissionError("operator-canonical-target-changed")
            commands = [
                ["git", "-C", str(path), "push", "origin", sha + ":refs/heads/" + ref]
            ]
        else:
            sha, kind = source_sha, "merge"
            commands = [
                [
                    "git",
                    "-C",
                    str(path),
                    "merge",
                    "--no-ff",
                    "-m",
                    "Merge branch '" + branch + "'",
                    sha,
                ]
            ]
    else:
        raise ValueError("operator-delivery-operation-invalid")
    safe_hooks(path)
    target = {
        **git_target(path),
        "ref": ref,
        "source_sha": source_sha,
        "operation": operation,
    }
    product = {"revision": revision(wt), "acceptance": digest(record["final_result"])}
    return kind, commands, target, product


def delivery_proposal(store: RunStore, run_id: str, operation: str) -> dict[str, Any]:
    return propose(store, run_id, *delivery_details(store, run_id, operation))


def delivery_execute(store: RunStore, run_id: str, token: str) -> dict[str, Any]:
    action = read_action(store, run_id, token)
    owner = FileLock(scope(store, run_id) / ".owner.lock")
    if not owner.acquire(blocking=False):
        raise PermissionError("operator-owner-active")
    try:
        with FileLock(
            store.home
            / "locks"
            / (
                "operator-target-"
                + digest(action["identity"]["target"]["path"])
                + ".lock"
            )
        ):
            return _delivery_execute(store, run_id, token)
    finally:
        owner.close()


def _delivery_execute(store: RunStore, run_id: str, token: str) -> dict[str, Any]:
    from kosu_defteri import redakte

    action = read_action(store, run_id, token)
    operation = action["identity"]["target"]["operation"]

    def current() -> dict[str, Any]:
        return identity(store, run_id, *delivery_details(store, run_id, operation))

    with permitted(store, run_id, token, current) as receipt:
        command = action["identity"]["commands"][0]
        from kosu import process_identity

        receipt["effect_seed"] = {"process": process_identity(os.getpid())}
        atomic_json(
            scope(store, run_id) / "operator-actions" / (token + ".json"), receipt
        )
        result = subprocess.run(
            command,
            env={**os.environ, "GIT_REFLOG_ACTION": "cadre-operator-" + token},
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                redakte(result.stdout),
                redakte(result.stderr),
            )
        path = Path(action["identity"]["target"]["path"])
        sha = (
            git(path, "rev-parse", "HEAD")
            if operation == "merge"
            else action["identity"]["target"]["head"]
        )
        if operation == "merge":
            parents = git(path, "rev-list", "--parents", "-n", "1", sha).split()
            if parents != [
                sha,
                action["identity"]["target"]["head"],
                action["identity"]["target"]["source_sha"],
            ]:
                raise PermissionError("operator-merge-result-unverified")
        receipt["result"] = {
            "operation": operation,
            "sha": sha,
            "path": str(path),
            "branch": action["identity"]["target"]["ref"],
            "source_sha": action["identity"]["target"]["source_sha"],
        }
    action = read_action(store, run_id, token)
    record_delivery(store, run_id, action)
    return action["result"]


def deliver(store: RunStore, run_id: str, operation: str) -> None:
    recover_delivery(store, run_id)
    record = scope_record(store, run_id)
    if not delivery_candidate(record):
        raise PermissionError("operator-reviewed-product-required")
    if not Path(record["worktree"]).exists():
        action = restore_proposal(store, run_id)
        restore_execute(store, run_id, obtain(store, run_id, action))
        record = scope_record(store, run_id)
    token = (record.get("final_result", {}).get("delivery") or {}).get("action_token")
    if (record.get("final_result", {}).get("delivery") or {}).get(
        "git_permission"
    ) == "required":
        if not isinstance(token, str):
            raise PermissionError("operator-commit-permission-unavailable")
        action = read_action(store, run_id, token)
        commit_execute(store, run_id, obtain(store, run_id, action))
        adopt_commit(store, run_id, read_action(store, run_id, token))
        record = scope_record(store, run_id)
    if operation == "merge" and (record.get("delivery_state") or {}).get("merge_sha"):
        operation = "canonical-push"
    action = delivery_proposal(store, run_id, operation)
    delivery_execute(store, run_id, obtain(store, run_id, action))
    if operation == "merge":
        action = delivery_proposal(store, run_id, "canonical-push")
        try:
            delivery_execute(store, run_id, obtain(store, run_id, action))
        except PermissionError as error:
            if str(error) != "operator-declined":
                raise


def setup_details(
    store: RunStore, run_id: str, wt: Path, command: str
) -> tuple[list[list[str]], dict[str, Any], dict[str, Any]]:
    from sonuc import revision

    wt = wt.resolve(strict=True)
    record = scope_record(store, run_id)
    baseline = (scope(store, run_id) / "baseline-source").resolve()
    if Path(record["worktree"] or ".").resolve() != wt and not (
        wt == baseline and record.get("source_sha") == git(wt, "rev-parse", "HEAD")
    ):
        raise PermissionError("operator-setup-target-invalid")
    return (
        [["bash", "-lc", command]],
        {"path": str(wt), "operation": "project-setup"},
        {"revision": revision(wt)},
    )


def initialized_empty(path: Path) -> bool:
    """FIX-46: a folder containing only `.git` and no HEAD (interrupted between init and commit) counts as empty.
    CR-33: `.git` + skeleton files matching the manifest (skeleton written, no commit yet) is the same interruption state."""
    try:
        entries = [p.name for p in path.iterdir()]
        if entries != [".git"] and not (".git" in entries and skeleton_pending(path)):
            return False
    except OSError:
        return False
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, timeout=30)
    return probe.returncode != 0


def skeleton_pending(path: Path) -> dict[str, Any] | None:
    import iskelet
    return iskelet.calisma_agaci_uyuyor(path)


def initialization_details(
    path: Path,
    iskelet_: dict[str, Any] | None = None,
) -> tuple[list[list[str]], dict[str, Any], dict[str, Any]]:
    path = path.resolve(strict=True)
    if any(path.iterdir()) and not initialized_empty(path):
        raise PermissionError("operator-initialization-empty-directory-required")
    if iskelet_ is not None:
        import iskelet
        if (not isinstance(iskelet_, dict) or iskelet_.get("tur") not in iskelet.DESTEK
                or not isinstance(iskelet_.get("ad"), str) or not iskelet_["ad"]):
            raise PermissionError("operator-initialization-skeleton-unsupported")
    for variable, setting in (
        ("GIT_AUTHOR_NAME", "user.name"),
        ("GIT_AUTHOR_EMAIL", "user.email"),
        ("GIT_COMMITTER_NAME", "user.name"),
        ("GIT_COMMITTER_EMAIL", "user.email"),
    ):
        configured = subprocess.run(
            ["git", "-C", str(path), "config", "--get", setting],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if not os.environ.get(variable) and (
            configured.returncode != 0 or not configured.stdout.strip()
        ):
            raise PermissionError("operator-git-author-required")
    author = git(path, "var", "GIT_AUTHOR_IDENT")
    committer = git(path, "var", "GIT_COMMITTER_IDENT")
    info = path.stat()
    git_prefix = ["git", "-c", "core.fsmonitor=false", "-C", str(path)]
    commands = [[*git_prefix, "init", "-q", "-b", "main"]]
    target: dict[str, Any] = {"path": str(path), "identity": [info.st_dev, info.st_ino]}
    if iskelet_ is None:
        commands.append([*git_prefix, "commit", "--allow-empty", "-q", "-m", "Initial baseline"])
    else:
        import iskelet
        tur, ad = iskelet_["tur"], iskelet_["ad"]
        manifest = iskelet.manifest(ad, tur)
        commands.append([sys.executable, str(Path(__file__).resolve().with_name("iskelet.py")), "yaz", str(path), ad, tur])
        commands.append([*git_prefix, "add", "-A"])
        commands.append([*git_prefix, "commit", "-q", "-m", f"Initial baseline (cadre {tur} iskeleti)"])
        target["iskelet"] = {"tur": tur, "ad": ad, "dosyalar": sorted([*manifest["dosyalar"], iskelet.MANIFEST]),
                             "manifest_sha256": iskelet.sha256(iskelet.manifest_metni(ad, tur)),
                             "sonra": [manifest["komutlar"][a] for a in iskelet.ADIMLAR]}
    return (
        commands,
        target,
        {"author": author.rsplit(" ", 2)[0], "committer": committer.rsplit(" ", 2)[0]},
    )


def initialization_proposal(store: RunStore, path: Path, iskelet_: dict[str, Any] | None = None) -> dict[str, Any]:
    return propose(store, "system", "init-commit", *initialization_details(path, iskelet_))


def initialization_execute(store: RunStore, token: str) -> None:
    action = read_action(store, "system", token)
    path = Path(action["identity"]["target"]["path"])
    iskelet_ = action["identity"]["target"].get("iskelet")
    iskelet_ = {"tur": iskelet_["tur"], "ad": iskelet_["ad"]} if iskelet_ else None
    with FileLock(
        store.home / "locks" / ("operator-target-" + digest(str(path)) + ".lock")
    ):
        with permitted(
            store,
            "system",
            token,
            lambda: identity(
                store, "system", "init-commit", *initialization_details(path, iskelet_)
            ),
        ):
            for command in action["identity"]["commands"]:
                if "commit" in command:
                    safe_hooks(path)
                subprocess.run(
                    command,
                    capture_output=True,
                    check=True,
                    timeout=30,
                    env={**os.environ, "GIT_REFLOG_ACTION": "cadre-operator-" + token},
                )


def initialization_recover(store: RunStore, path: Path) -> None:
    path = path.resolve(strict=True)
    for action in pending(store, "system"):
        target = action["identity"]["target"]
        if (
            action["identity"]["kind"] != "init-commit"
            or target["path"] != str(path)
            or action["status"] not in ("in-flight", "uncertain")
        ):
            continue
        with FileLock(
            store.home / "locks" / ("operator-target-" + digest(str(path)) + ".lock")
        ):
            with FileLock(scope(store, "system") / ".operator-actions.lock"):
                action = read_action(store, "system", action["token"])
                info = path.stat()
                token = action["token"]
                safe_hooks(path)
                if subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "-q", "HEAD"],
                                  capture_output=True, timeout=30).returncode != 0:
                    if [info.st_dev, info.st_ino] != target["identity"] or not initialized_empty(path):
                        raise PermissionError("operator-initialization-effect-unresolved")
                    action.update(status="failed", finished=time.time(),
                                  resolution={"outcome": "init-without-commit", "previous": action["status"], "ts": time.time()})
                    atomic_json(scope(store, "system") / "operator-actions" / (token + ".json"), action)
                    continue
                head = git(path, "rev-parse", "HEAD")
                reflog = git(path, "reflog", "-1", "--format=%gs")
                skeleton = target.get("iskelet")
                if skeleton:
                    import iskelet
                    tree_ok = (iskelet.commit_agaci_uyuyor(path, head) or {}).get("ad") == skeleton["ad"]
                else:
                    tree_ok = not git(path, "ls-tree", "-r", "--name-only", head)
                if (
                    [info.st_dev, info.st_ino] != target["identity"]
                    or len({m["id"] for m in action["messages"]}) != 2
                    or git(path, "rev-list", "--parents", "-n", "1", head).split()
                    != [head]
                    or not tree_ok
                    or git(path, "branch", "--show-current") != "main"
                    or git(path, "show", "-s", "--format=%an <%ae>", head)
                    != action["identity"]["product"]["author"]
                    or git(path, "show", "-s", "--format=%cn <%ce>", head)
                    != action["identity"]["product"]["committer"]
                    or not any(
                        reflog.startswith("cadre-operator-" + token + suffix)
                        for suffix in (":", " (initial):")
                    )
                ):
                    raise PermissionError("operator-initialization-effect-unresolved")
                action.update(
                    status="completed",
                    finished=time.time(),
                    reconciled=True,
                    result={"head": head},
                )
                atomic_json(
                    scope(store, "system") / "operator-actions" / (token + ".json"),
                    action,
                )


def model_details(
    store: RunStore, run_id: str
) -> tuple[list[list[str]], dict[str, Any], dict[str, Any]]:
    from kosu import original_request
    from runtime_sync import guard_version

    record = scope_record(store, run_id)
    if record.get("proje_ust") or record.get("request"):
        raise PermissionError("operator-model-root-required")
    command = [
        "bash",
        str(Path(__file__).resolve().parents[1] / "bin/orkestra.sh"),
        record["project"],
        str(scope(store, run_id) / "prompt.md"),
    ]
    target = {
        "project": record["project"],
        "source_sha": record.get("source_sha"),
        "providers": ["opencode", "opencode-go", "openrouter"],
        "duration_minutes": 60,
        "budget_usd": record["budget_cap_usd"],
        "protocol": guard_version(Path(__file__).resolve().parents[1]),
    }
    return (
        [command],
        target,
        {
            "request_sha256": hashlib.sha256(
                original_request(store, run_id).encode()
            ).hexdigest()
        },
    )


def model_activate(store: RunStore, run_id: str, token: str) -> None:
    action = read_action(store, run_id, token)
    if action["status"] != "completed":
        with permitted(
            store,
            run_id,
            token,
            lambda: identity(
                store, run_id, "model-calls", *model_details(store, run_id)
            ),
        ):
            pass
    store.update(run_id, operator_model_permission=token)
    waiting = "project-tool-operator-model-approval-required"
    children = [r["run_id"] for r in store.list() if r.get("proje_ust") == run_id]
    for rid in [run_id, *children]:
        record = store.read(rid)
        if record.get("tool_boundary_wait") == waiting or str(record.get("reason") or "") in (
            waiting, "operator-model-approval-required", "proje-durdu: " + waiting
        ):
            store.update(rid, tool_boundary_wait="", reason="operator-model-ready")


def planlama_cagrisi(record: dict[str, Any]) -> bool:
    claim = record.get("plan_review_claim") or {}
    gorev = os.environ.get("CADRE_GOREV_ID", "")
    changes = record.get("plan_changes") or []
    return (
        gorev.startswith(("plan-input-", "plan-review-"))
        and claim.get("task_id") == gorev
        and claim.get("revision") == len(record.get("plan_history") or [])
        and bool(changes)
        and changes[-1].get("status") != "stop_requested"
    )


def model_check(store: RunStore, run_id: str, provider: str, model: str = "") -> None:
    from kosu import original_request, request_authorized, istek_suresi_doldu
    from runtime_sync import guard_version

    record = scope_record(store, run_id)
    while record.get("proje_ust"):
        if (scope(store, record["run_id"]) / "cancel.flag").exists() or plan_hold(
            record
        ):
            raise PermissionError("operator-model-stopped")
        record = scope_record(store, record["proje_ust"])
    root_id = record["run_id"]
    if (
        record["state"] in ("completed", "cancelled", "failed")
        or (plan_hold(record) and not planlama_cagrisi(record))
        or (scope(store, root_id) / "cancel.flag").exists()
    ):
        raise PermissionError("operator-model-stopped")
    if record.get("request"):
        approval = record.get("request_permission") or {}
        action = approval.get("action") or {}
        if (
            not request_authorized(record, provider + "/" + model if model else "")
            or provider not in action.get("providers", [])
            or istek_suresi_doldu(record)
        ):
            raise PermissionError("operator-model-approval-required")
        return
    action = read_action(store, root_id, record.get("operator_model_permission") or "")
    target = action["identity"]["target"]
    if (
        action["status"] != "completed"
        or action["identity"]["kind"] != "model-calls"
        or len({message["id"] for message in action["messages"]}) != 2
        or provider not in target["providers"]
        or float(record["budget_cap_usd"]) > float(target["budget_usd"])
        or record.get("source_sha") != target["source_sha"]
        or guard_version(Path(__file__).resolve().parents[1]) != target["protocol"]
        or hashlib.sha256(original_request(store, root_id).encode()).hexdigest()
        != action["identity"]["product"]["request_sha256"]
        or time.time() > action["finished"] + target["duration_minutes"] * 60
    ):
        raise PermissionError("operator-model-approval-required")


def setup_proposal(
    store: RunStore, run_id: str, wt: Path, command: str
) -> dict[str, Any]:
    return propose(store, run_id, "install", *setup_details(store, run_id, wt, command))


def cleanup_details(
    store: RunStore, run_id: str, tree: Path
) -> tuple[list[list[str]], dict[str, Any], dict[str, Any]]:
    from arsiv import fingerprint, index

    record = scope_record(store, run_id)
    tree = tree.resolve(strict=True)
    if (
        record["state"] != "completed"
        or Path(record["worktree"]).resolve() != tree
        or not tree.is_relative_to(
            (store.home / "worktrees" / record["project"]).resolve()
        )
    ):
        raise PermissionError("operator-cleanup-target-invalid")
    info = tree.stat()
    return (
        [["cadre", "cleanup", run_id]],
        {"path": str(tree), "identity": [info.st_dev, info.st_ino]},
        {"fingerprint": fingerprint(index(tree))},
    )


def cleanup_proposal(store: RunStore, run_id: str, tree: Path) -> dict[str, Any]:
    return propose(store, run_id, "cleanup", *cleanup_details(store, run_id, tree))


def restore_details(
    store: RunStore, run_id: str
) -> tuple[list[list[str]], dict[str, Any], dict[str, Any]]:
    from arsiv import verify

    record = scope_record(store, run_id)
    cleanup = record.get("cleanup") or {}
    raw = Path(cleanup["worktree_archive"])
    manifest = raw.resolve(strict=True)
    expected = (store.home / "archives" / run_id / "worktree").resolve()
    if (
        raw.is_symlink()
        or manifest.parent != expected
        or Path(record["worktree"]).exists()
        or not Path(record["worktree"])
        .resolve()
        .is_relative_to((store.home / "worktrees" / record["project"]).resolve())
    ):
        raise PermissionError("operator-restore-target-invalid")
    verify(manifest)
    return (
        [["cadre", "restore", run_id]],
        {"path": record["worktree"], "manifest": str(manifest)},
        {"manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()},
    )


def restore_proposal(store: RunStore, run_id: str) -> dict[str, Any]:
    return propose(store, run_id, "restore", *restore_details(store, run_id))


def restore_execute(store: RunStore, run_id: str, token: str) -> None:
    from arsiv import restore_run_worktree

    owner = FileLock(scope(store, run_id) / ".owner.lock")
    if not owner.acquire(blocking=False):
        raise PermissionError("operator-owner-active")
    try:

        def current() -> dict[str, Any]:
            return identity(store, run_id, "restore", *restore_details(store, run_id))

        with permitted(store, run_id, token, current):
            restore_run_worktree(store, run_id)
    finally:
        owner.close()


def main(argv: list[str]) -> int:
    store = RunStore()
    if len(argv) == 2 and argv[0] == "initialize":
        action = initialization_proposal(store, Path(argv[1]))
        initialization_execute(store, obtain(store, "system", action))
        return 0
    if len(argv) in (3, 4) and argv[0] == "model-check":
        try:
            model_check(store, argv[1], argv[2], argv[3] if len(argv) == 4 else "")
        except (PermissionError, ValueError):
            record = store.read(argv[1])
            if not record.get("request") and record["state"] not in (
                "completed",
                "failed",
                "cancelled",
            ):
                root_id = record.get("proje_ust") or argv[1]
                propose(store, root_id, "model-calls", *model_details(store, root_id))
                for rid in {argv[1], root_id}:
                    store.update(
                        rid,
                        tool_boundary_wait="project-tool-operator-model-approval-required",
                        reason="operator-model-approval-required",
                    )
            raise
        return 0
    if len(argv) == 2 and argv[0] == "model-consent":
        action = propose(store, argv[1], "model-calls", *model_details(store, argv[1]))
        model_activate(store, argv[1], obtain(store, argv[1], action))
        return 0
    if len(argv) == 2 and argv[0] == "cleanup-backup":
        from rotasyon import backup_delete_proposal, backup_delete_execute

        with FileLock(store.home / "backups/opencode-db/.backup.lock"):
            action = backup_delete_proposal(store.home, Path(argv[1]))
            backup_delete_execute(store.home, obtain(store, "system", action))
        return 0
    if len(argv) == 2 and argv[0] in ("cleanup", "restore"):
        command, run_id = argv
        if command == "restore":
            action = restore_proposal(store, run_id)
            restore_execute(store, run_id, obtain(store, run_id, action))
        else:
            from rotasyon import rotate

            action = cleanup_proposal(
                store, run_id, Path(scope_record(store, run_id)["worktree"])
            )
            obtain(store, run_id, action)
            if rotate(store.home, keep=0, run_id=run_id)["archived"] != 1:
                raise PermissionError("operator-cleanup-waiting")
        return 0
    if len(argv) == 3 and argv[0] == "deliver":
        deliver(store, argv[1], argv[2])
        print(
            "merge edildi; teslim aşaması kaydedildi."
            if argv[2] == "merge"
            else "Teslim aşaması kaydedildi."
        )
        return 0
    if len(argv) == 4 and argv[0] == "resolve":
        if os.environ.get("CADRE_RUN_ID"):
            raise PermissionError("operator-command-required")
        print(json.dumps(resolve(store, argv[1], argv[2], argv[3]), ensure_ascii=False))
        return 0
    if len(argv) < 3:
        raise ValueError("operator-command-arguments")
    command, run_id, token = argv[:3]
    if command == "approve":
        result = approve(store, run_id, token, argv[3] if len(argv) == 4 else "")
    elif command in ("commit", "checkpoint"):
        if command == "checkpoint":
            action = read_action(store, run_id, token)
            if (
                len(argv) < 5
                or str(Path(argv[3]).resolve()) != action["identity"]["target"]["path"]
                or argv[4] != scope_record(store, run_id)["project"]
            ):
                raise PermissionError("operator-commit-worktree-invalid")
        result = commit_execute(store, run_id, token)
    elif command == "adopt-commit":
        adopt_commit(store, run_id, read_action(store, run_id, token))
        result = {"status": "adopted"}
    elif command == "show":
        result = read_action(store, run_id, token)
    else:
        raise ValueError("operator-command-invalid")
    print(
        result["files"]
        if command == "checkpoint"
        else json.dumps(result, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    import sys

    try:
        raise SystemExit(main(sys.argv[1:]))
    except (
        OSError,
        ValueError,
        KeyError,
        EOFError,
        subprocess.SubprocessError,
    ) as error:
        reasons = {
            "operator-action-stale": "onaydan sonra ürün değişti; yeniden onay gerekir",
            "operator-reviewed-product-required": "onaydan sonra ürün değişti veya kabul kanıtı eksik",
            "operator-canonical-dirty": "ana repoda commit'lenmemiş değişiklik var",
            "operator-canonical-branch-changed": "ana repo kayıtlı ana dalında olmalı",
        }
        print(
            reasons.get(str(error), type(error).__name__ + ": operator-action-blocked"),
            file=sys.stderr,
        )
        raise SystemExit(78)
