#!/usr/bin/env python3
"""Plan-JSON helpers for the bash dispatcher: validation, ordering, per-task time caps."""
# dispatch_util.py — plan-JSON helpers of cadre-dispatch.sh.
# Usage: dispatch_util.py <command> [arg] ; reads the plan JSON from STDIN.
# this file instead of inline python, to avoid bash 3.2's nested quote/brace traps.
import json, os, sys, datetime, pathlib, re
import math
import unicodedata
from run_store import RunStore, INTERNAL_TASKS, identifier
from outcome import validate_uncertainties

MODELS = {'auto', 'ds-pro', 'ds-pro-go', 'ds-flash', 'glm-flash', 'cl-last', 'cl-opus', 'cl-fable'}
# D12: a task going to a PAID model requires estimate_usd > 0; the free alias (gem-flash) was removed in 4c-11.
FREE_MODELS = {'auto'}   # auto: the router picks from the chain by difficulty; no estimate required


def plan_path(value, root=None):
    """Validates a plan file path and returns its normalized form (B95).
    Path detail (`./x`, `src/`, `a//b`) does not reject the plan; escapes, absolute paths and symlinks are rejected."""
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError('invalid file path')
    value = value.strip().strip('"\'')
    # B282: the planner writes `path (description)`; when the description was taken as part of the path, the scope check dropped the real file.
    described = re.match(r'(\S.*?)\s+\(.*\)$', value)
    if described:
        value = described.group(1).strip('"\'')
    while value.startswith('./'):
        value = value[2:]
    while '//' in value:
        value = value.replace('//', '/')
    value = value.rstrip('/')
    if not value:
        raise ValueError('invalid file path')
    parts = value.split('/')
    if any(p in ('', '.', '..', '.git') for p in parts) or any(c in value for c in '\\,:*?[]'):
        raise ValueError('file path must be explicit and relative')
    if root is not None:
        root = pathlib.Path(root).resolve(strict=True)
        path = root
        try:
            for part in parts:
                path = path/part
                if path.is_symlink():
                    raise ValueError('plan paths cannot traverse symlinks')
            if not path.resolve().is_relative_to(root):
                raise ValueError('plan path escaped worktree')
        except (OSError, RuntimeError) as exc:
            raise ValueError('unverifiable plan path') from exc
    return value


def _number(value):
    """Converts a purely numeric string to int/float; returns None for what cannot be converted (bool, broken text).
    K9: this is a TYPE correction, no value is invented — '3'->3 keeps its meaning, '1; false' is left untouched."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
    return None


def normalize_plan(plan):
    """K9: meaning-preserving mechanical normalization (numeric string -> number, whitespace, None list -> []); a schema violation is fixed without going back to the model.
    Dependencies, acceptance and cost are never invented: if missing/invalid the validator rejects."""
    if not isinstance(plan, dict):
        return plan
    for task in plan.get('tasks') or []:
        if not isinstance(task, dict):
            continue
        if isinstance(task.get('model'), str):
            task['model'] = task['model'].strip()
        for key in ('difficulty', 'parallel_group', 'estimate_min'):
            v = _number(task.get(key))
            if isinstance(v, int):
                task[key] = v
        v = _number(task.get('estimate_usd'))
        if isinstance(v, (int, float)):
            task['estimate_usd'] = v
        # None lists mean 'none' (empty list); no dependency/scope declaration is invented.
        # B340: scope_outside is not a required field and is not added if missing. 4c-9: no sub-agent in a lane; the field that comes out of old habit is dropped.
        task.pop('sub_agents', None)
        for key in ('dependent', 'scope_outside'):
            if key in task and task[key] is None:
                task[key] = []
    return plan


def validate_plan(plan, root=None):
    normalize_plan(plan)
    if not isinstance(plan, dict):
        raise ValueError('plan must be an object')
    if not isinstance(plan.get('job_id'), str):
        raise ValueError('invalid job identity')
    identifier(plan['job_id'])
    validate_uncertainties(plan)
    if 'project_features' in plan:
        from project_mode import _verify, project_ref_sources
        features = _verify(plan['project_features'])
        if features != project_ref_sources(plan, features):
            raise ValueError('project-source-coverage')
    if plan.get('kind') == 'verified-backlog':
        items = plan.get('source_items')
        if (plan.get('tasks') != [] or plan.get('merge_order', []) != []
                or not isinstance(items, list) or not items
                or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
                       or not item['id'] or item.get('status') != 'verified' or item.get('tasks') != []
                       or not isinstance(item.get('evidence'), str) or not item['evidence'].strip()
                       or not isinstance(item.get('source_sha256'), str)
                       or not re.fullmatch('[0-9a-f]{64}', item['source_sha256']) for item in items)
                or len({item['id'] for item in items}) != len(items)):
            raise ValueError('invalid verified backlog plan')
        return []
    if plan.get('kind') == 'project' and plan.get('tasks') == []:
        from project_mode import _verify
        features = _verify(plan.get('project_features'))
        return [f['id'] for f in features]
    order = ordered_tasks(plan)
    if len(order) > 100:
        raise ValueError('plan too large')
    for task in plan['tasks']:
        for key in ('definition', 'acceptance'):
            if not isinstance(task.get(key), str) or not task[key].strip() or len(task[key]) > 100000:
                raise ValueError('invalid task description or acceptance')
        if task.get('model') not in MODELS:
            raise ValueError('unknown task model')
        for key, lower, upper in (('difficulty', 1, 5), ('parallel_group', 0, 100), ('estimate_min', 1, 1440)):
            if type(task.get(key)) is not int or not lower <= task[key] <= upper:
                raise ValueError('invalid task numeric field')
        cost = task.get('estimate_usd', 0)
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise ValueError('invalid task estimate')
        # D12: a task going to a paid model needs a real cost estimate; a zero estimate kills the dynamic cap (Σ×1.5).
        if task.get('model') not in FREE_MODELS and cost <= 0:
            raise ValueError(f"paid task {task.get('id', '?')} (model {task.get('model')}) needs estimate_usd > 0")
        names = task.get('files')
        if not isinstance(names, list) or not names or len(names) > 1000:
            raise ValueError('explicit file list required')
        # B95: normalized paths are written back into the plan; downstream (lane prompt, scope, secret scan)
        # always sees the same form. Duplicates that resolve to the same path after normalization (`./a.py` + `a.py`) are silently
        # merged — no information is lost; a duplicate of two different files is still an error.
        clean = []
        for name in names:
            path = plan_path(name, root)
            if path not in clean:
                clean.append(path)
        task['files'] = names = clean
        excluded = task.get('scope_outside', [])
        if not isinstance(excluded, list) or any(not isinstance(x, str) for x in excluded):
            raise ValueError('invalid exclusions')
    merge = plan.get('merge_order', order)
    if not isinstance(merge, list) or any(not isinstance(x, str) for x in merge) or sorted(merge) != sorted(order):
        raise ValueError('invalid merge order')
    quality = plan.get('quality', {})
    if not isinstance(quality, dict) or any(not isinstance(quality.get(k, ''), str) for k in ('model', 'scope')):
        raise ValueError('invalid review description')
    return order


def extract_plan(text):
    decoder = json.JSONDecoder()
    plans = []
    index = 0
    while index < len(text):
        start = text.find('{', index)
        if start < 0:
            break
        try:
            value, length = decoder.raw_decode(text[start:])
            index = start + length
        except ValueError:
            index = start + 1
            continue
        if isinstance(value, dict) and 'tasks' in value:
            validate_plan(value)
            plans.append(value)
    if len(plans) != 1:
        raise ValueError('one unambiguous valid plan required')
    return plans[0]


def main_order(plan, order=None):
    """G-E10: order of the main lane loop; sub-tasks (with a parent) run only inside the split task."""
    sub = {g.get('id') for g in plan.get('tasks') or [] if isinstance(g, dict) and g.get('parent')}
    return [gid for gid in (order if order is not None else ordered_tasks(plan)) if gid not in sub]


def ordered_tasks(plan):
    tasks = plan.get('tasks')
    if tasks == [] and plan.get('kind') == 'verified-backlog':
        return validate_plan(plan)
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('empty or invalid task list')
    by_id = {}
    for task in tasks:
        gid = task.get('id') if isinstance(task, dict) else None
        if not isinstance(gid, str) or not re.fullmatch(r'[a-z0-9_-]{1,32}', gid) or gid in by_id or gid in INTERNAL_TASKS:
            raise ValueError('invalid or duplicate task identity')
        by_id[gid] = task
    dependencies = {}
    for gid, task in by_id.items():
        needs = task.get('dependent', [])
        if not isinstance(needs, list) or any(not isinstance(n, str) or n not in by_id or n == gid for n in needs):
            raise ValueError('unknown or invalid dependency')
        dependencies[gid] = set(needs)
    result = []
    pending = list(by_id)
    while pending:
        ready = next((gid for gid in pending if dependencies[gid].issubset(result)), None)
        if ready is None:
            raise ValueError('cyclic task dependencies')
        result.append(ready)
        pending.remove(ready)
    return result


def path_identity(value: str, root: pathlib.Path) -> tuple[tuple, ...]:
    value = plan_path(value, root)
    path = root.resolve(strict=True)
    parts = []
    missing = False
    sensitive = True
    for name in value.split('/'):
        if not missing:
            parent = path
            path = path / name
            try:
                info = path.stat()
            except FileNotFoundError:
                if sys.platform == 'darwin':
                    policy = os.pathconf(parent, 11)
                    if policy not in (0, 1):
                        raise ValueError('filesystem-name-policy-unknown')
                    sensitive = bool(policy)
                elif sys.platform.startswith('linux'):
                    import array, fcntl
                    flags = array.array('l', [0])
                    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        fcntl.ioctl(fd, (2 << 30) | (flags.itemsize << 16) | (ord('f') << 8) | 1, flags)
                    finally:
                        os.close(fd)
                    sensitive = not bool(flags[0] & 0x40000000)
                else:
                    raise ValueError('filesystem-name-policy-unknown')
                missing = True
            else:
                parts.append(('inode', info.st_dev, info.st_ino))
                continue
        if sys.platform == 'darwin':
            name = unicodedata.normalize('NFD', name)
        parts.append(('name', name if sensitive else name.casefold()))
    return tuple(parts)


def parallel_candidates(plan, gid, n, status, root=None):
    """B340: lanes that can start together with gid: no dependents, no dependencies between them, no outcome yet, disjoint files.
    `parallel_group` carries no meaning; the deciding rule is 'empty dependencies + disjoint files'."""
    tasks = {g['id']: g for g in plan['tasks']}
    order = main_order(plan)
    root = pathlib.Path(root) if root is not None else pathlib.Path.cwd()
    identities = {}
    for task in tasks.values():
        try:
            names = task.get('files') or []
            identities[task['id']] = [path_identity(name, root) for name in names] if names else None
        except (OSError, ValueError, RuntimeError):
            identities[task['id']] = None

    def overlap(a, b):
        left, right = identities[a['id']], identities[b['id']]
        if left is None or right is None:
            return True
        return any(x[:len(y)] == y or y[:len(x)] == x or
                   (x[-1][0] == y[-1][0] == 'inode' and x[-1] == y[-1]) for x in left for y in right)
    main = tasks.get(gid)
    if main is None or gid not in order:
        return []
    selected = [main]
    for og in order[order.index(gid) + 1:]:
        g = tasks[og]
        if len(selected) >= n:
            break
        if (status.get(og) or {}).get('dispatch_result'):
            continue
        if any((status.get(d) or {}).get('dispatch_result') != 'produced' for d in g.get('dependent') or []):
            continue
        if any(g['id'] in (s.get('dependent') or []) or s['id'] in (g.get('dependent') or []) or overlap(g, s)
               for s in selected):
            continue
        selected.append(g)
    return [g['id'] for g in selected[1:]]


def _task_environment(files):
    """CR-47: the lane prompt carries the recorded QA command, interpreter, product root and write limit; empty without a record."""
    try:
        from run_store import task_environment
        record = json.loads((pathlib.Path(os.environ['CADRE_RUN_DIR']) / 'run.json').read_text())
        return task_environment(RunStore(), record, list(files))
    except (KeyError, OSError, ValueError, AttributeError, TypeError):
        return ''


def _report_declaration():
    """Report declaration in the acceptance record (G-R1); the report module is loaded only for a report job."""
    try:
        return json.loads((pathlib.Path(os.environ['CADRE_RUN_DIR']) / 'run.json').read_text()).get('report') or None
    except (KeyError, OSError, ValueError, AttributeError):
        return None


def shared_document_error(plan):
    """B328: when the shared record document was in every task the planner serialized independent jobs; it goes only in the last task.
    Applied only when accepting a NEW plan: rejecting a recorded plan retroactively would fail a running run."""
    tasks = plan.get('tasks', [])
    last = (plan.get('merge_order') or [g.get('id') for g in tasks] or [None])[-1]
    # B343: except the CONTRACT — the contract guard wants the line of a new engine file in the SAME task
    shared = [g['id'] for g in tasks if any(n in ('BACKLOG.md', 'README.md') for n in g.get('files', []))]
    if len(tasks) > 1 and (len(shared) > 1 or (shared and shared[0] != last)):
        return 'shared registry (BACKLOG.md/README.md) only in the last task: ' + ','.join(shared)
    return None


def main():
    if sys.argv[1:2] == ['extract']:
        try:
            plan = extract_plan(sys.stdin.read())
            if len(sys.argv) > 2:   # G-E9: if a rule floor is given, the level's plan limit is also a schema rule
                from level import plan_limit
                error = plan_limit(plan, int(sys.argv[2]))
                if error:
                    raise ValueError(error)
            error = shared_document_error(plan)
            if error:
                raise ValueError(error)
            declaration = _report_declaration()
            if declaration:   # G-R1: in a report job the plan only touches the report + evidence appendix
                from report_mode import plan_audit
                error = plan_audit(plan, declaration)
                if error:
                    raise ValueError(error)
            print(json.dumps(plan, ensure_ascii=False))
        except ValueError as exc:
            # B95: a schema violation is NOT A MODEL FAILURE. A fixed one-line format + a separate exit code so the caller can
            # tell it apart from a traceback; the message is given to the next attempt as a correction.
            print(f'SCHEMA: {exc}', file=sys.stderr)
            raise SystemExit(2)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "cit_clean":
        # S5 finding: light-task direct-API output came wrapped in a ```py ... ``` fence -> 14 lint errors in the .py file.
        # Only an outer fence wrapping ALL the content is removed; inner fences are untouched. (stdin = raw content, not the plan JSON)
        import re as _re
        t = sys.stdin.read().strip("\n")
        mm = _re.match(r"^\s*```[A-Za-z0-9_+.-]*[ \t]*\n(.*?)\n```\s*$", t, _re.S)
        print(mm.group(1) if mm else t)
        return
    cmd = sys.argv[1]
    d = json.load(sys.stdin)
    if cmd == 'validate':
        print(' '.join(main_order(d, validate_plan(d, sys.argv[2] if len(sys.argv) > 2 else None))))
    elif cmd == 'path-verify':
        root = pathlib.Path(sys.argv[2])
        validate_plan(d, root)
    elif cmd == "summary":
        print(len(d["tasks"]), "task; models:", [g["model"] for g in d["tasks"]])
    elif cmd == "gids":
        print(" ".join(g["id"] for g in d["tasks"]))
    elif cmd == "order":
        print(" ".join(ordered_tasks(d)))
    elif cmd == "dependent":
        print(" ".join(next(g for g in d['tasks'] if g['id'] == sys.argv[2]).get('dependent', [])))
    elif cmd == "groups":
        print(" ".join(sorted({str(g["parallel_group"]) for g in d["tasks"]})))
    elif cmd == "group_gids":
        grp = sys.argv[2]
        print(" ".join(g["id"] for g in d["tasks"] if str(g["parallel_group"]) == grp))
    elif cmd == "model":
        gid = sys.argv[2]
        print([g["model"] for g in d["tasks"] if g["id"] == gid][0])
    elif cmd == "abort":
        gid = sys.argv[2]
        g = [x for x in d["tasks"] if x["id"] == gid][0]
        print(max(int(g["estimate_min"]) * 3 * 60, 1800))
    elif cmd == "job_id":
        print(d["job_id"])
    elif cmd == "order":
        print(",".join(d.get("merge_order", [])))
    elif cmd == "quality_model":
        print(d.get("quality", {}).get("model", "own"))
    elif cmd == "parallel":   # DESIGN-5(2): independent cloud lanes that may run together with sys.argv[2] (at most argv[3] in total)
        record = RunStore().read(os.environ['CADRE_RUN_ID'])
        print(' '.join(parallel_candidates(d, sys.argv[2], int(sys.argv[3]), record['tasks'], root=record.get('worktree') or pathlib.Path.cwd())))
    elif cmd == "quality_scope":
        print(d.get("quality", {}).get("scope", "general"))
    elif cmd == "task_pf":
        gid = sys.argv[2]
        g = [x for x in d["tasks"] if x["id"] == gid][0]
        environment = _task_environment(g['files'])
        tool = ('only the restricted shell (bash) is open, the edit/write/read/websearch tools are closed' if environment
                else 'use the websearch tool if a web search is needed')
        # A fixed first line raises the cached prefix ratio; its explanation does not enter the prompt, and if it did it would leak into the product (B217).
        print(f"""[QE-TASK v1]
TASK ({g['id']}): {g['definition']}
FILES (touch ONLY these): {', '.join(g['files'])}
ACCEPTANCE CRITERION (you are measured by this): {g.get('acceptance', 'the change works and the tests pass')}
SCOPE-OUTSIDE (NEVER touch): {', '.join(g.get('scope_outside', []) or ['-'])}
DEPENDENCIES: {', '.join(g.get('dependent', []) or ['none'])}
RULES: do NOT delegate with task() — do the work yourself; {tool}. Do NOT touch any other file.
GIT: NO commit/add/stash/checkout in this lane — parallel lanes share the same working tree; the coordinator merges and commits.
{environment + chr(10) + 'When done, test the change with the QA COMMAND under ENVIRONMENT.' if environment else 'When done, test the change (run the test if there is one).'}
{(pathlib.Path(__file__).resolve().parent/"standard-task-form.txt").read_text().strip()}""")
        declaration = _report_declaration()
        if declaration:   # G-R2: every lane sees the evidence appendix format and the id rule
            from report_mode import lane_rules
            print(lane_rules(declaration))
    elif cmd == "status_first":
        project_ref = sys.argv[2]
        print(json.dumps({
            "project_ref": project_ref, "job_id": d["job_id"],
            "dispatch_ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "tasks": {g["id"]: {"status": "waiting", "assigned_model": g["model"],
                                    "attempt_counter": 0, "quality_step": 0}
                         for g in d["tasks"]}}, ensure_ascii=False))
    elif cmd == "status_update":
        # All writers update the same locked run record; no shadow state file.
        run_id, gid, state = sys.argv[2:5]
        RunStore().task_update(run_id, gid, state=state)
    else:
        sys.exit(2)

if __name__ == "__main__":
    main()
