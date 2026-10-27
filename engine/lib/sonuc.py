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


ONBELLEK_ARTIGI = re.compile(r'(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules/\.cache)(/|$)|\.pyc$')
PROTECTED_SOURCE = re.compile(r'(^|/)([.]env[^/]*|[.]ssh|[.]aws|[.]kube|[.]netrc|[.]npmrc|[.]pypirc|[.]claude[.]json|auth[.]json|config[.]local([.][^/]*)?|credentials[^/]*|secrets?|kubeconfig[^/]*|id_rsa[^/]*|id_ed25519[^/]*|[.]imza-[^/]*|[^/]*-anahtar)(/|$)|[.](pem|key|p12|pfx)$', re.I)
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
            if name == '.git' or protected_source(relative) or ONBELLEK_ARTIGI.search(relative):
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
            if not raw_name or ONBELLEK_ARTIGI.search(raw_name.decode('utf-8', errors='surrogateescape')):
                continue   # G-C2 (B156): the QA/compiler cache is not product; it made the first QA round ERROR in vain
            name = raw_name.decode('utf-8', errors='surrogateescape')
            path = root/name
            # A symlink's identity is its target text; never read external contents.
            content = str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
            digest.update(len(raw_name).to_bytes(8, 'big') + raw_name)
            digest.update((path.lstat().st_mode & 0o777).to_bytes(4, 'big'))
            digest.update(hashlib.sha256(content).digest())
        try:   # B158(e): a file written and hidden via .git/info/exclude in the same step had to change the identity (QA sees it, delivery does not)
            yol = Path(git('rev-parse', '--git-path', 'info/exclude').decode().strip())
            yol = yol if yol.is_absolute() else root / yol
            digest.update(b'info/exclude' + hashlib.sha256(yol.read_bytes()).digest())
        except (OSError, subprocess.SubprocessError):
            pass
        if git('rev-parse', 'HEAD').decode().strip() != head:
            raise RuntimeError('HEAD changed while recording product identity')
        return {'head': head, 'digest': digest.hexdigest()}


def _json_adaylari(text: str):
    """B39 → B174: judges embed the JSON inside prose and the prose has braces like `{package_version()}`;
    the "outermost {...}" tolerance tripped over them (2/3 cloud judges wrote a valid GECER verdict in vain). Candidates in order: the whole
    text, ```json fences, `raw_decode` from every `{` position. Only a packaging tolerance — the identity/verdict/scope check does not change."""
    yield from _tam_adaylar(text)
    decoder = json.JSONDecoder()
    onarilmis = _kacis_onar(text)
    for metin in (text, onarilmis) if onarilmis != text else (text,):
        for m in re.finditer(r"\{", metin):
            try:
                value, _ = decoder.raw_decode(metin, m.start())
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def _tam_adaylar(text: str):
    try:
        yield json.loads(text)
    except ValueError:
        pass
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def _kacis_onar(text: str) -> str:
    def onar(m: re.Match) -> str:
        kacis = m.group(1)
        if len(kacis) == 5 or kacis in '"\\/bfnrt':
            return m.group(0)
        return "'" if kacis == "'" else '\\\\' + kacis
    return re.sub(r'\\(u[0-9a-fA-F]{4}|.)', onar, text, flags=re.S)


def _dis_json_hatasi(text: str) -> ValueError | None:
    """B312: if the outer object starting with the first '{' cannot be decoded, inner objects become candidates and the reason comes out as a misleading 'no identity'."""
    bas = text.find('{')
    if bas < 0:
        return None
    try:
        json.JSONDecoder().raw_decode(text, bas)
        return None
    except ValueError as e:
        return ValueError(f'review JSON invalid: {getattr(e, "msg", e)} (char {getattr(e, "pos", "?")})')


def tek_json_nesnesi(text: str, anahtar: str) -> dict[str, Any]:
    adaylar: list[dict[str, Any]] = []
    for aday in _json_adaylari(text):
        if isinstance(aday, dict) and anahtar in aday and aday not in adaylar:
            adaylar.append(aday)
    if len(adaylar) != 1:
        raise ValueError('review-output-invalid')
    return adaylar[0]


def _review_json(text: str) -> Any:
    """First JSON candidate (backward compatibility); `parse_review` passes all candidates through the meaning check."""
    for value in _json_adaylari(text):
        return value
    raise ValueError('review JSON missing')


def ayni_revizyon(yazilan: Any, digest: str) -> bool:
    y = str(yazilan or '').strip().lower()
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


def _json_incele(value: Any, expected: dict[str, str], ids: list[str], *, planning: bool = False) -> dict[str, Any]:
    if planning and isinstance(value, dict):
        validate_uncertainties(value)
    if not isinstance(value, dict) or not ayni_revizyon(value.get('revision'), expected['digest']):
        raise ValueError('review product identity missing or different')
    if value.get('verdict') not in ('GECER', 'DUZELT'):
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
        if yer_tutucu(item['evidence']):
            raise ValueError('placeholder evidence')   # G-C5 (B153)
        if item.get('eksik') not in (None, '', 'urun', 'kanit'):
            raise ValueError('invalid gap kind')   # eksik: urun (behaviour defect) | kanit (engine/process evidence missing)
        seen.add(item['id'])
    passed = value['verdict'] == 'GECER' and all(item['status'] == 'PASS' for item in criteria)
    result = {'status': 'PASS' if passed else 'FAIL', 'revision': expected,
              'verdict': value['verdict'], 'acceptance': criteria}
    if planning:
        result.update(decisions=value.get('decisions', []), assumptions=value.get('assumptions', []))
        if result['decisions']:
            result.update(status='WAITING', reason='plan-decision-required')
    return result


SATIR_KARAR = re.compile(r'^\s*KARAR\s*:\s*(GECER|DUZELT)\s*$', re.M | re.I)
SATIR_REVIZYON = re.compile(r'^\s*REVIZYON\s*:\s*([0-9a-f]{6,})\s*$', re.M | re.I)
SATIR_KRITER = re.compile(r'^\s*KRITER\s+(\S+)\s*:\s*(PASS|FAIL)\s*[-—:]\s*(.+?)\s*$', re.M | re.I)


def yer_tutucu(kanit: str) -> bool:
    """G-C5 (B153): template patterns like `<somut kanit>`, `...`, `<...>` are not evidence."""
    k = kanit.strip()
    return not k or k in ('...', '…') or (k.startswith('<') and k.endswith('>')) or 'somut kanit' in k.lower()


def satir_incelemesi(text: str, expected: dict[str, str], ids: list[str]) -> dict[str, Any]:
    """Line-based judge contract (for a judge that cannot produce JSON): `REVIZYON: <digest>` · `KARAR: GECER|DUZELT` ·
    `KRITER <id>: PASS|FAIL - <evidence>`. The check is as strict as JSON: the revision matches, every criterion exactly once and with evidence."""
    revisions = SATIR_REVIZYON.findall(text)
    if (len(revisions) != 1 or not ayni_revizyon(revisions[0], expected['digest'])
            or len(re.findall(r'^\s*REVIZYON\s*:', text, re.M | re.I)) != 1):
        raise ValueError('review product identity missing or different')
    kararlar = SATIR_KARAR.findall(text)
    if len(kararlar) != 1 or len(re.findall(r'^\s*KARAR\s*:', text, re.M | re.I)) != 1:
        raise ValueError('review verdict missing or invalid')
    kriterler = []
    gorulen = set()
    for m in SATIR_KRITER.finditer(text):
        kimlik, durum, kanit = m.group(1).strip().strip(':'), m.group(2).upper(), m.group(3).strip()
        if kimlik not in ids or kimlik in gorulen or not kanit:
            raise ValueError('unknown or duplicate acceptance criterion')
        if yer_tutucu(kanit):
            raise ValueError('placeholder evidence')   # G-C5 (B153): a template echo is not a review
        gorulen.add(kimlik)
        kriterler.append({'id': kimlik, 'status': durum, 'evidence': kanit})
    if not ids or len(kriterler) != len(ids):
        raise ValueError('acceptance coverage incomplete')
    verdict = kararlar[0].upper()
    passed = verdict == 'GECER' and all(k['status'] == 'PASS' for k in kriterler)
    return {'status': 'PASS' if passed else 'FAIL', 'revision': expected,
            'verdict': verdict, 'acceptance': kriterler, 'format': 'satir'}


def parse_review(raw: str, expected: dict[str, str], ids: list[str], *, planning: bool = False) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith('```json\n') and text.endswith('\n```'):
        text = text[8:-4]
    try:
        hata: Exception | None = None
        tam_hata: Exception | None = None
        tamlar = list(_tam_adaylar(text))
        gecerli = []
        for aday in _json_adaylari(text):        # B174: every candidate goes through the same meaning check; the first error stays as the reason
            try:
                gecerli.append(_json_incele(aday, expected, ids, planning=planning))
            except (ValueError, KeyError, TypeError) as e:
                if planning and str(e) == 'invalid-plan-uncertainty':
                    return {'status': 'ERROR', 'revision': expected, 'reason': 'invalid-plan-uncertainty'}
                if planning and isinstance(aday, dict) and aday.get('decisions'):
                    result = {'status': 'ERROR', 'revision': expected, 'reason': str(e)}
                    if ayni_revizyon(aday.get('revision'), expected['digest']):
                        result.update(decisions=aday['decisions'], assumptions=aday.get('assumptions', []))
                    return result
                hata = hata or e
                if aday in tamlar:
                    tam_hata = tam_hata or e
        if gecerli:
            imza = {(g['verdict'], tuple(sorted((k['id'], k['status']) for k in g['acceptance'])),
                     json.dumps(g.get('decisions', []), sort_keys=True)) for g in gecerli}
            if len(imza) > 1:   # B183: contradictory verdicts (draft GECER + final DUZELT) produce no acceptance, fail-closed
                return {'status': 'ERROR', 'revision': expected, 'reason': 'conflicting review verdicts'}
            return gecerli[-1]
        raise tam_hata or _dis_json_hatasi(text) or hata or ValueError('review JSON missing')
    except (ValueError, KeyError, TypeError) as error:
        try:                                   # if the JSON did not hold, try the line contract
            return satir_incelemesi(text, expected, ids)
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
        bagli = {'files': list(scope.get('files') or []), 'tests': list(scope.get('tests') or [])}
        disari = sorted(set(scope.get('changed') or []) - set(bagli['files']))
        if disari:
            return {'status': 'FAIL', 'reason': 'scope-files-exceeded', 'out_of_scope': disari, 'scope': bagli,
                    'revision': current, 'qa': qa, 'review': review}
        kosan = set(discovery.get('observed_files') or []) if discovery.get('status') == 'PASS' else set()
        eksik = sorted(set(bagli['tests']) - kosan)
        if eksik:
            return {'status': 'FAIL', 'reason': 'scope-tests-not-run', 'missing_tests': eksik, 'scope': bagli,
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
    if qa.get('status') == 'PASS' and qa.get('kind') != 'rapor' and (qa.get('discovery') or {}).get('status') != 'PASS':
        return {**result, 'status': 'FAIL', 'reason': 'qa-discovery-unverified'}
    if not passed:
        if qa.get('status') != 'PASS':
            result['reason'] = 'qa-not-passed'
        elif review.get('status') != 'PASS':
            fails = [a for a in (review.get('acceptance') or []) if isinstance(a, dict) and a.get('status') == 'FAIL']
            # Missing evidence: the product behaviour is right, the engine/process evidence is missing; no code repair is opened, it is re-reviewed once the evidence is complete.
            result['reason'] = 'review-evidence-gap' if fails and all(a.get('eksik') == 'kanit' for a in fails) else 'review-not-passed'
        elif review.get('independent') is not True:
            result['reason'] = 'reviewer-not-independent'
        else:
            result['reason'] = 'revision-mismatch'
    return result


def approved(record: dict[str, Any]) -> bool:
    result = record.get('final_result') or {}
    try:
        from kabul import review_valid
        if not review_valid(record, result.get('review') or {}):
            return False
        from kosu import bounded_json, original_request, plan_digest, plan_product, run_job_type
        if run_job_type(record) == 'review':
            from kosu import RunStore, plan_hold
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
            anlam = {a.get('id'): a.get('status') for a in result['review'].get('acceptance') or [] if isinstance(a, dict)}
            return (artifact.get('binding') == binding and plan_digest(artifact) == delivery.get('digest')
                    and anlam.get('iddia_destegi') == anlam.get('istek_kapsami') == 'PASS'
                    and artifact.get('result') == result and result.get('status') == 'PASS'
                    and review.get('status') == 'PASS' and review.get('plan_digest') == binding['plan_digest']
                    and result['qa'].get('job_type') == 'review'
                    and artifact['session_id'] != result['review']['session_id']
                    and decision(result['qa'], result['review'], product)['status'] == 'PASS')
        current = revision(record['worktree'])
        report_profile = bool(record.get('rapor'))
        if (result.get('qa', {}).get('kind') == 'rapor') != report_profile:
            return False
        if not report_profile:
            from qa_kaniti import evidence_valid
            from qa_bakim import command_for, evidence
            from kabul import ek_kabul_ile
            if not evidence_valid(result['qa']) or result['qa']['command'] != ek_kabul_ile(command_for(record), record):
                return False
            if evidence(record, result['qa'])['status'] == 'FAIL':
                return False
        from kabul import degisen_dosyalar, istek_kapsami, kurulum_izinli, kurulum_izleri, tasinan_degismemis_dosyalar
        if kurulum_izleri(record['worktree']) and not kurulum_izinli(record):
            return False
        scope = istek_kapsami(record)
        if scope:
            scope = {**scope, 'changed': sorted(set(degisen_dosyalar(record['worktree'], record['source_sha']))
                                         - tasinan_degismemis_dosyalar(record['worktree'], record))}
        return result.get('status') == 'PASS' and decision(result['qa'], result['review'], current, scope=scope)['status'] == 'PASS'
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        return False


def implemented(record: dict[str, Any]) -> bool:
    """K6: intermediate implementation evidence does not replace final acceptance."""
    result = record.get('final_result') or {}
    if not record.get('proje_ust') or not record.get('feature_acceptance'):
        return False
    try:
        current = revision(record['worktree'])
        qa = result.get('qa') or {}
        report_profile = bool(record.get('rapor'))
        if (qa.get('kind') == 'rapor') != report_profile:
            return False
        if not report_profile:
            from qa_kaniti import evidence_valid
            if not evidence_valid(qa):
                return False
        return (result.get('status') == 'UYGULANDI' and result.get('revision') == current
                and qa.get('status') == 'PASS' and qa.get('revision') == current
                and bool(qa.get('command'))
                and (not result.get('riskli') or decision(qa, result.get('review') or {}, current, allow_targeted=True)['status'] == 'PASS'))
    except (KeyError, ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        return False
