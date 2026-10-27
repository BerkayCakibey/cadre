"""Immutable product identity, the single acceptance predicate and judge-output parsing (K4)."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import tempfile
import subprocess
from typing import Any, Iterator


CACHE_SURPLUS = re.compile(r'(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules/\.cache)(/|$)|\.pyc$')
PROTECTED_SOURCE = re.compile(r'(^|/)([.]env[^/]*|[.]ssh|[.]aws|[.]kube|[.]netrc|[.]npmrc|[.]pypirc|[.]claude[.]json|auth[.]json|config[.]local([.][^/]*)?|credentials[^/]*|secrets?|kubeconfig[^/]*|id_rsa[^/]*|id_ed25519[^/]*|[.]signature-[^/]*|[^/]*-key)(/|$)|[.](pem|key|p12|pfx)$', re.I)
TEMPLATE_SOURCE = re.compile(r'(^|/)[.]env[.](example|sample|template)$', re.I)


def protected_source(name: str) -> bool:
    """FIX-01 (N-IN04/N-ENV02): `.env.example/.sample/.template` templates do not count as secrets in the product identity and the archive; a real `.env*` is still rejected."""
    return bool(PROTECTED_SOURCE.search(name)) and not TEMPLATE_SOURCE.search(name)



def input_revision(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('input-source-symlink')
    digest, size, count = hashlib.sha256(), 0, 0
    for index, (directory, dirs, files, fd) in enumerate(os.fwalk(root, follow_symlinks=False)):
        if index > 10_000:
            raise ValueError('input-source-limit')
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '.venv', 'node_modules', '__pycache__') and not protected_source(d))
        files += [name for name in dirs if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)]
        for name in sorted(files):
            relative = str((Path(directory) / name).relative_to(root))
            if name == '.git' or protected_source(relative) or CACHE_SURPLUS.search(relative):
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raw = os.readlink(name, dir_fd=fd).encode()
            else:
                if info.st_nlink != 1:
                    raise ValueError('input-source-hardlink')
                stream_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(stream_fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 2_000_000:
                        raise ValueError('input-source-limit')
                    raw = stream.read(2_000_001)
                    after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('input-source-changed')
            count, size = count + 1, size + len(raw)
            if count > 10_000 or size > 8_000_000 or len(raw) > 2_000_000:
                raise ValueError('input-source-limit')
            digest.update(relative.encode() + b'\0' + str(stat.S_IFMT(info.st_mode)).encode() + b'\0' + hashlib.sha256(raw).digest())
    return {'head': None, 'digest': digest.hexdigest(), 'files': count}


@contextmanager
def git_read_environment(root: str | Path) -> Iterator[dict[str, str]]:
    root = Path(root)
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    index = subprocess.run(['git', '-C', str(root), 'rev-parse', '--git-path', 'index'], env=env,
                           capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    source = Path(index) if Path(index).is_absolute() else root / index
    with tempfile.TemporaryDirectory(prefix='cadre-git-read-') as temporary:
        target = Path(temporary) / 'index'
        if source.exists():
            shutil.copy2(source, target)
        yield {**env, 'GIT_INDEX_FILE': str(target)}


def revision(worktree: str | Path) -> dict[str, str]:
    root = Path(worktree)
    with git_read_environment(root) as env:
        def git(*args: str) -> bytes:
            return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args], check=True,
                                  capture_output=True, timeout=30, env=env).stdout
        names = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')
        for raw_name in names:
            if not raw_name:
                continue
            name = os.fsdecode(raw_name)
            if protected_source(name):
                raise ValueError('product-protected-source')
            path = root / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError('product-source-hardlink')
        head = git('rev-parse', 'HEAD').decode().strip()
        digest = hashlib.sha256()
        digest.update(head.encode())
        digest.update(git('diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--'))
        for raw_name in sorted(git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')):
            if not raw_name or CACHE_SURPLUS.search(raw_name.decode('utf-8', errors='surrogateescape')):
                continue   # G-C2 (B156): the QA/compiler cache is not product; it made the first QA round ERROR in vain
            name = raw_name.decode('utf-8', errors='surrogateescape')
            path = root/name
            # A symlink's identity is its target text; never read external contents.
            content = str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
            digest.update(len(raw_name).to_bytes(8, 'big') + raw_name)
            digest.update((path.lstat().st_mode & 0o777).to_bytes(4, 'big'))
            digest.update(hashlib.sha256(content).digest())
        try:   # B158(e): a file written and hidden via .git/info/exclude in the same step had to change the identity (QA sees it, delivery does not)
            pathname = Path(git('rev-parse', '--git-path', 'info/exclude').decode().strip())
            pathname = pathname if pathname.is_absolute() else root / pathname
            digest.update(b'info/exclude' + hashlib.sha256(pathname.read_bytes()).digest())
        except (OSError, subprocess.SubprocessError):
            pass
        if git('rev-parse', 'HEAD').decode().strip() != head:
            raise RuntimeError('HEAD changed while recording product identity')
        return {'head': head, 'digest': digest.hexdigest()}


def _json_candidates(text: str):
    """B39 → B174: judges embed the JSON inside prose and the prose has braces like `{package_version()}`;
    the "outermost {...}" tolerance tripped over them (2/3 cloud judges wrote a valid PASS verdict in vain). Candidates in order: the whole
    text, ```json fences, `raw_decode` from every `{` position. Only a packaging tolerance — the identity/verdict/scope check does not change."""
    yield from _full_candidates(text)
    decoder = json.JSONDecoder()
    repaired = _escape_repair(text)
    for body in (text, repaired) if repaired != text else (text,):
        for m in re.finditer(r"\{", body):
            try:
                value, _ = decoder.raw_decode(body, m.start())
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def _full_candidates(text: str):
    try:
        yield json.loads(text)
    except ValueError:
        pass
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def _escape_repair(text: str) -> str:
    def repair(m: re.Match) -> str:
        escape = m.group(1)
        if len(escape) == 5 or escape in '"\\/bfnrt':
            return m.group(0)
        return "'" if escape == "'" else '\\\\' + escape
    return re.sub(r'\\(u[0-9a-fA-F]{4}|.)', repair, text, flags=re.S)


def _outside_json_error(text: str) -> ValueError | None:
    """B312: if the outer object starting with the first '{' cannot be decoded, inner objects become candidates and the reason comes out as a misleading 'no identity'."""
    start = text.find('{')
    if start < 0:
        return None
    try:
        json.JSONDecoder().raw_decode(text, start)
        return None
    except ValueError as e:
        return ValueError(f'review JSON invalid: {getattr(e, "msg", e)} (char {getattr(e, "pos", "?")})')


def single_json_object(text: str, key: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for candidate in _json_candidates(text):
        if isinstance(candidate, dict) and key in candidate and candidate not in candidates:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError('review-output-invalid')
    return candidates[0]


def _review_json(text: str) -> Any:
    """First JSON candidate (backward compatibility); `parse_review` passes all candidates through the meaning check."""
    for value in _json_candidates(text):
        return value
    raise ValueError('review JSON missing')


def same_revision(written: Any, digest: str) -> bool:
    y = str(written or '').strip().lower()
    return y == digest or (len(y) >= 16 and re.fullmatch(r'[0-9a-f]+', y) is not None and y[:16] == digest[:16])


def validate_uncertainties(value: dict[str, Any]) -> None:
    seen = set()
    for key, fields in (('assumptions', ('choice', 'reason')), ('decisions', ('kind', 'question', 'reason'))):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 32:
            raise ValueError('invalid-plan-uncertainty')
        for item in items:
            if not isinstance(item, dict) or set(item) != {'id', *fields}:
                raise ValueError('invalid-plan-uncertainty')
            if not isinstance(item['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item['id']):
                raise ValueError('invalid-plan-uncertainty')
            if item['id'] in seen or any(not isinstance(item[f], str) or not item[f].strip()
                    or len(item[f]) > 4000 or '\x00' in item[f] for f in fields):
                raise ValueError('invalid-plan-uncertainty')
            if key == 'decisions' and item['kind'] not in ('purpose', 'scope', 'authority', 'budget', 'ownership', 'data-loss', 'evidence'):
                raise ValueError('invalid-plan-uncertainty')
            seen.add(item['id'])


def merge_uncertainties(*values: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for value in values:
        validate_uncertainties(value)
        for key in ('assumptions', 'decisions'):
            if key not in value:
                continue
            items = result.setdefault(key, [])
            for item in value[key]:
                previous = next((saved for saved in items if saved['id'] == item['id']), None)
                if previous is not None and previous != item:
                    raise ValueError('invalid-plan-uncertainty')
                if previous is None:
                    items.append(dict(item))
    validate_uncertainties(result)
    return result


def _json_review(value: Any, expected: dict[str, str], ids: list[str], *, planning: bool = False) -> dict[str, Any]:
    if planning and isinstance(value, dict):
        validate_uncertainties(value)
    if not isinstance(value, dict) or not same_revision(value.get('revision'), expected['digest']):
        raise ValueError('review product identity missing or different')
    if value.get('verdict') not in ('PASS', 'REVISE'):
        raise ValueError('review verdict missing or invalid')
    # B39: models echo the key name 'criteria' from the input (gemini 2x, live 01:59/02:24); same schema, same check.
    criteria = value.get('acceptance', value.get('criteria'))
    if not isinstance(criteria, list) or not ids or len(criteria) != len(ids):
        raise ValueError('acceptance coverage incomplete')
    seen = set()
    for item in criteria:
        if not isinstance(item, dict) or item.get('id') not in ids or item['id'] in seen:
            raise ValueError('unknown or duplicate acceptance criterion')
        if item.get('status') not in ('PASS', 'FAIL') or not isinstance(item.get('evidence'), str) or not item['evidence'].strip():
            raise ValueError('acceptance status/evidence missing')
        if placeholder(item['evidence']):
            raise ValueError('placeholder evidence')   # G-C5 (B153)
        if item.get('missing') not in (None, '', 'product', 'evidence'):
            raise ValueError('invalid gap kind')   # missing: product (behaviour defect) | evidence (engine/process evidence missing)
        seen.add(item['id'])
    passed = value['verdict'] == 'PASS' and all(item['status'] == 'PASS' for item in criteria)
    result = {'status': 'PASS' if passed else 'FAIL', 'revision': expected,
              'verdict': value['verdict'], 'acceptance': criteria}
    if planning:
        result.update(decisions=value.get('decisions', []), assumptions=value.get('assumptions', []))
        if result['decisions']:
            result.update(status='WAITING', reason='plan-decision-required')
    return result


LINE_DECISION = re.compile(r'^\s*DECISION\s*:\s*(PASS|REVISE)\s*$', re.M | re.I | re.ASCII)
LINE_REVISION = re.compile(r'^\s*REVISION\s*:\s*([0-9a-f]{6,})\s*$', re.M | re.I | re.ASCII)
LINE_CRITERION = re.compile(r'^\s*CRITERION\s+(\S+)\s*:\s*(PASS|FAIL)\s*[-—:]\s*(.+?)\s*$', re.M | re.I | re.ASCII)


def placeholder(evidence: str) -> bool:
    """G-C5 (B153): template patterns like `<concrete evidence>`, `...`, `<...>` are not evidence."""
    k = evidence.strip()
    return not k or k in ('...', '…') or (k.startswith('<') and k.endswith('>')) or 'concrete evidence' in k.lower()


def line_review(text: str, expected: dict[str, str], ids: list[str]) -> dict[str, Any]:
    """Line-based judge contract (for a judge that cannot produce JSON): `REVISION: <digest>` · `DECISION: PASS|REVISE` ·
    `CRITERION <id>: PASS|FAIL - <evidence>`. The check is as strict as JSON: the revision matches, every criterion exactly once and with evidence."""
    revisions = LINE_REVISION.findall(text)
    if (len(revisions) != 1 or not same_revision(revisions[0], expected['digest'])
            or len(re.findall(r'^\s*REVISION\s*:', text, re.M | re.I | re.ASCII)) != 1):
        raise ValueError('review product identity missing or different')
    decisions = LINE_DECISION.findall(text)
    if len(decisions) != 1 or len(re.findall(r'^\s*DECISION\s*:', text, re.M | re.I | re.ASCII)) != 1:
        raise ValueError('review verdict missing or invalid')
    criteria = []
    seen = set()
    for m in LINE_CRITERION.finditer(text):
        id, status, evidence = m.group(1).strip().strip(':'), m.group(2).upper(), m.group(3).strip()
        if id not in ids or id in seen or not evidence:
            raise ValueError('unknown or duplicate acceptance criterion')
        if placeholder(evidence):
            raise ValueError('placeholder evidence')   # G-C5 (B153): a template echo is not a review
        seen.add(id)
        criteria.append({'id': id, 'status': status, 'evidence': evidence})
    if not ids or len(criteria) != len(ids):
        raise ValueError('acceptance coverage incomplete')
    verdict = decisions[0].upper()
    passed = verdict == 'PASS' and all(k['status'] == 'PASS' for k in criteria)
    return {'status': 'PASS' if passed else 'FAIL', 'revision': expected,
            'verdict': verdict, 'acceptance': criteria, 'format': 'line'}


def parse_review(raw: str, expected: dict[str, str], ids: list[str], *, planning: bool = False) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith('```json\n') and text.endswith('\n```'):
        text = text[8:-4]
    try:
        failure: Exception | None = None
        full_error: Exception | None = None
        integers = list(_full_candidates(text))
        valid = []
        for candidate in _json_candidates(text):        # B174: every candidate goes through the same meaning check; the first error stays as the reason
            try:
                valid.append(_json_review(candidate, expected, ids, planning=planning))
            except (ValueError, KeyError, TypeError) as e:
                if planning and str(e) == 'invalid-plan-uncertainty':
                    return {'status': 'ERROR', 'revision': expected, 'reason': 'invalid-plan-uncertainty'}
                if planning and isinstance(candidate, dict) and candidate.get('decisions'):
                    result = {'status': 'ERROR', 'revision': expected, 'reason': str(e)}
                    if same_revision(candidate.get('revision'), expected['digest']):
                        result.update(decisions=candidate['decisions'], assumptions=candidate.get('assumptions', []))
                    return result
                failure = failure or e
                if candidate in integers:
                    full_error = full_error or e
        if valid:
            signature = {(g['verdict'], tuple(sorted((k['id'], k['status']) for k in g['acceptance'])),
                     json.dumps(g.get('decisions', []), sort_keys=True)) for g in valid}
            if len(signature) > 1:   # B183: contradictory verdicts (draft PASS + final REVISE) produce no acceptance, fail-closed
                return {'status': 'ERROR', 'revision': expected, 'reason': 'conflicting review verdicts'}
            return valid[-1]
        raise full_error or _outside_json_error(text) or failure or ValueError('review JSON missing')
    except (ValueError, KeyError, TypeError) as error:
        try:                                   # if the JSON did not hold, try the line contract
            return line_review(text, expected, ids)
        except (ValueError, KeyError, TypeError):
            return {'status': 'ERROR', 'revision': expected, 'reason': str(error)}


def compare_baseline(baseline: dict[str, Any], current: dict[str, Any], before: str, after: str) -> dict[str, Any]:
    if baseline.get('command') != current.get('command'):
        return {'status': 'UNKNOWN', 'reason': 'different-qa-scope'}
    if baseline.get('status') not in ('PASS', 'FAIL') or current.get('status') not in ('PASS', 'FAIL'):
        return {'status': 'UNKNOWN', 'reason': 'incomplete-qa-evidence'}
    if current['status'] == 'PASS':
        return {'status': 'PASS', 'reason': 'baseline-recovered' if baseline['status'] == 'FAIL' else 'no-regression'}
    if baseline['status'] == 'PASS':
        return {'status': 'FAIL', 'reason': 'new-regression'}
    def failures(raw: str) -> dict[str, str]:
        found = {}
        for match in re.finditer(r'^(?:FAIL|ERROR): (.+)\n([\s\S]*?)(?=^(?:FAIL|ERROR): |^Ran \d|\Z)', raw, re.M):
            errors = re.findall(r'^[\w.]+(?:Error|Exception|Failure)(?::[^\n]*)?$', match[2], re.M)
            if errors:
                found[match[1]] = errors[-1]
        for name, message in re.findall(r'^(?:FAILED|ERROR) (\S+::\S+) - ([^\n]+)', raw, re.M):
            found[name] = message
        return found
    old, new = failures(before), failures(after)
    if not old or not new:
        return {'status': 'UNKNOWN', 'reason': 'failure-identities-missing'}
    changed = {name for name in old.keys() & new.keys() if old[name] != new[name]}
    return {'status': 'FAIL', 'reason': 'new-regression' if new.keys() - old.keys() or changed else 'pre-existing-failures',
            'existing': sorted((new.keys() & old.keys()) - changed), 'introduced': sorted(new.keys() - old.keys()),
            'changed': sorted(changed), 'resolved': sorted(old.keys() - new.keys())}


def decision(qa: dict[str, Any], review: dict[str, Any], current: dict[str, str], *, allow_targeted: bool = False,
             scope: dict[str, Any] | None = None) -> dict[str, Any]:
    passed = (qa.get('status') == review.get('status') == 'PASS'
              and review.get('independent') is True
              and qa.get('revision') == review.get('revision') == current)
    discovery = qa.get('discovery') or {}
    if scope is not None:
        # CR-35 (user decision 20.10): a narrow scope bound to the request/approval. A change outside the allowed files and a required test that
        # did not run close acceptance; only this binding lets targeted discovery stand in for the full package.
        bound = {'files': list(scope.get('files') or []), 'tests': list(scope.get('tests') or [])}
        outside = sorted(set(scope.get('changed') or []) - set(bound['files']))
        if outside:
            return {'status': 'FAIL', 'reason': 'scope-files-exceeded', 'out_of_scope': outside, 'scope': bound,
                    'revision': current, 'qa': qa, 'review': review}
        running = set(discovery.get('observed_files') or []) if discovery.get('status') == 'PASS' else set()
        missing = sorted(set(bound['tests']) - running)
        if missing:
            return {'status': 'FAIL', 'reason': 'scope-tests-not-run', 'missing_tests': missing, 'scope': bound,
                    'revision': current, 'qa': qa, 'review': review}
        allow_targeted = True
    if discovery.get('scope') == 'targeted' and not allow_targeted:
        return {'status': 'FAIL', 'reason': 'full-qa-required', 'revision': current, 'qa': qa, 'review': review}
    if (qa.get('test_maintenance') or {}).get('status') == 'PASS':
        if not any(c.get('id') == 'authorized_test_maintenance' and c.get('status') == 'PASS' for c in review.get('acceptance', [])):
            return {'status': 'FAIL', 'reason': 'test-maintenance-review-required', 'revision': current, 'qa': qa, 'review': review}
    result: dict[str, Any] = {'status': 'PASS' if passed else 'FAIL', 'revision': current, 'qa': qa, 'review': review}
    if scope is not None:
        result['scope'] = {'files': list(scope.get('files') or []), 'tests': list(scope.get('tests') or [])}
    if qa.get('status') == 'PASS' and qa.get('kind') != 'report' and (qa.get('discovery') or {}).get('status') != 'PASS':
        return {**result, 'status': 'FAIL', 'reason': 'qa-discovery-unverified'}
    if not passed:
        if qa.get('status') != 'PASS':
            result['reason'] = 'qa-not-passed'
        elif review.get('status') != 'PASS':
            fails = [a for a in (review.get('acceptance') or []) if isinstance(a, dict) and a.get('status') == 'FAIL']
            # Missing evidence: the product behaviour is right, the engine/process evidence is missing; no code repair is opened, it is re-reviewed once the evidence is complete.
            result['reason'] = 'review-evidence-gap' if fails and all(a.get('missing') == 'evidence' for a in fails) else 'review-not-passed'
        elif review.get('independent') is not True:
            result['reason'] = 'reviewer-not-independent'
        else:
            result['reason'] = 'revision-mismatch'
    return result


def approved(record: dict[str, Any]) -> bool:
    result = record.get('final_result') or {}
    try:
        from acceptance import review_valid
        if not review_valid(record, result.get('review') or {}):
            return False
        from run_store import bounded_json, original_request, plan_digest, plan_product, run_job_type
        if run_job_type(record) == 'review':
            from run_store import RunStore, plan_hold
            directory = Path(record['prompt']).parent
            delivery = record.get('review_delivery') or {}
            if delivery.get('artifact') != str(directory / 'review-delivery.json') or delivery.get('status') != 'PASS' or plan_hold(record):
                return False
            artifact = bounded_json(directory / 'review-delivery.json')
            source = Path(record.get('worktree') or record.get('request', {}).get('target_path')
                          or (directory.parent.parent / 'projects' / record['project'] / 'repo-path').read_text().strip())
            product = plan_product(source)
            original = original_request(RunStore(directory.parent.parent), record['run_id'])
            plan = bounded_json(directory / 'plan.json')
            binding = {'source': product, 'source_path': str(source), 'request_sha256': hashlib.sha256(original.encode()).hexdigest(),
                       'request_hash': record.get('request_input_hash'), 'plan_digest': plan_digest(plan)}
            review = (record.get('plan_reviews') or [{}])[-1]
            meaning = {a.get('id'): a.get('status') for a in result['review'].get('acceptance') or [] if isinstance(a, dict)}
            return (artifact.get('binding') == binding and plan_digest(artifact) == delivery.get('digest')
                    and meaning.get('claim_support') == meaning.get('request_scope') == 'PASS'
                    and artifact.get('result') == result and result.get('status') == 'PASS'
                    and review.get('status') == 'PASS' and review.get('plan_digest') == binding['plan_digest']
                    and result['qa'].get('job_type') == 'review'
                    and artifact['session_id'] != result['review']['session_id']
                    and decision(result['qa'], result['review'], product)['status'] == 'PASS')
        current = revision(record['worktree'])
        report_profile = bool(record.get('report'))
        if (result.get('qa', {}).get('kind') == 'report') != report_profile:
            return False
        if not report_profile:
            from qa_evidence import evidence_valid
            from qa_maintenance import command_for, evidence
            from acceptance import with_extra_acceptance
            if not evidence_valid(result['qa']) or result['qa']['command'] != with_extra_acceptance(command_for(record), record):
                return False
            if evidence(record, result['qa'])['status'] == 'FAIL':
                return False
        from acceptance import changed_files, request_scope, install_allowed, install_traces, carried_unchanged_files
        if install_traces(record['worktree']) and not install_allowed(record):
            return False
        scope = request_scope(record)
        if scope:
            scope = {**scope, 'changed': sorted(set(changed_files(record['worktree'], record['source_sha']))
                                         - carried_unchanged_files(record['worktree'], record))}
        return result.get('status') == 'PASS' and decision(result['qa'], result['review'], current, scope=scope)['status'] == 'PASS'
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        return False


def implemented(record: dict[str, Any]) -> bool:
    """K6: intermediate implementation evidence does not replace final acceptance."""
    result = record.get('final_result') or {}
    if not record.get('project_ref_parent') or not record.get('feature_acceptance'):
        return False
    try:
        current = revision(record['worktree'])
        qa = result.get('qa') or {}
        report_profile = bool(record.get('report'))
        if (qa.get('kind') == 'report') != report_profile:
            return False
        if not report_profile:
            from qa_evidence import evidence_valid
            if not evidence_valid(qa):
                return False
        return (result.get('status') == 'APPLIED' and result.get('revision') == current
                and qa.get('status') == 'PASS' and qa.get('revision') == current
                and bool(qa.get('command'))
                and (not result.get('risky') or decision(qa, result.get('review') or {}, current, allow_targeted=True)['status'] == 'PASS'))
    except (KeyError, ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        return False
