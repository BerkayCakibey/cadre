"""Recorded operator decisions for test maintenance (deleted or weakened tests)."""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
import time
from typing import Any

from sonuc import revision


def validate(record: dict[str, Any], value: Any, command: str) -> None:
    if not isinstance(value, dict) or set(value) != {'source_sha', 'revision', 'command', 'reason', 'changes'}:
        raise ValueError('test-maintenance-schema')
    if value['source_sha'] != record['source_sha'] or value['revision'] != revision(record['worktree']):
        raise ValueError('test-maintenance-stale-revision')
    if value['command'] != command:
        raise ValueError('test-maintenance-command-changed')
    if not isinstance(value['reason'], str) or not 10 <= len(value['reason'].strip()) <= 4000:
        raise ValueError('test-maintenance-reason-required')
    if not isinstance(value['changes'], list) or not 1 <= len(value['changes']) <= 100:
        raise ValueError('test-maintenance-changes-required')
    seen = set()
    for change in value['changes']:
        if not isinstance(change, dict) or set(change) != {'path', 'purpose', 'preserved_criterion', 'replacement', 'positive', 'negative', 'boundary'}:
            raise ValueError('test-maintenance-change-schema')
        path = change['path']
        if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts or path in seen:
            raise ValueError('test-maintenance-path-invalid')
        seen.add(path)
        if any(not isinstance(change[k], str) or not change[k].strip() for k in ('purpose', 'preserved_criterion')):
            raise ValueError('test-maintenance-criterion-required')
        for key in ('replacement', 'positive', 'negative', 'boundary'):
            ids = change[key]
            if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(i, str) or not i.strip() for i in ids):
                raise ValueError('test-maintenance-evidence-required')
        if not set(change['positive'] + change['negative'] + change['boundary']) <= set(change['replacement']):
            raise ValueError('test-maintenance-replacement-mismatch')
        if not set(change['negative']) - set(change['positive']):
            raise ValueError('test-maintenance-negative-evidence-required')


def command_for(record: dict[str, Any]) -> str:
    from kosu import bounded_bytes
    path = record.get('constraints', {}).get('qa-komut')
    return bounded_bytes(Path(path), 100_000).decode().strip() if path else ''


def authorize(store: Any, rid: str, value: Any) -> dict[str, Any]:
    from kosu import FileLock, TERMINAL, atomic_json
    with ExitStack() as stack:
        stack.enter_context(FileLock(store.path(rid) / '.test-maintenance.lock'))
        for path in [store.path(rid) / '.owner.lock', *sorted((store.path(rid) / 'tasks').glob('*/.*.lock'))]:
            lock = FileLock(path)
            if not lock.acquire(blocking=False):
                lock.close()
                raise ValueError('test-maintenance-active-run')
            stack.enter_context(lock)
        stack.enter_context(FileLock(store.path(rid) / '.record.lock'))
        record = store.read(rid)
        if record['state'] in TERMINAL or (store.path(rid) / 'cancel.flag').exists():
            raise ValueError('test-maintenance-terminal-run')
        if record.get('feature_acceptance') or record.get('rapor'):
            raise ValueError('test-maintenance-full-code-run-required')
        validate(record, value, command_for(record))
        previous = record.get('test_maintenance')
        if previous and previous.get('decision') == value:
            return previous
        if previous and previous.get('decision', {}).get('revision') == value['revision']:
            raise ValueError('test-maintenance-decision-conflict')
        approved = {'origin': 'operator', 'decision': value, 'created': time.time(),
                    'request_hash': hashlib.sha256(Path(record['prompt']).read_bytes()).hexdigest()}
        from kosu import mudahale_ekle
        mudahale_ekle(record, 'test-maintenance')
        record.update(test_maintenance=approved, updated=time.time())
        atomic_json(store.path(rid) / 'run.json', record)
        return approved


def evidence(record: dict[str, Any], qa: dict[str, Any]) -> dict[str, Any]:
    approved = record.get('test_maintenance')
    if not approved:
        return {'status': 'NOT_APPLICABLE', 'paths': []}
    try:
        value = approved['decision']
        validate(record, value, qa.get('command', ''))
        if approved['origin'] != 'operator' or approved['request_hash'] != hashlib.sha256(Path(record['prompt']).read_bytes()).hexdigest():
            raise ValueError('test-maintenance-authority-changed')
        discovery = qa.get('discovery') or {}
        if qa.get('status') != 'PASS' or discovery.get('status') != 'PASS':
            raise ValueError('test-maintenance-discovery-required')
        passed = set(discovery.get('passed') or [])
        if any(not set(change['replacement']) <= passed for change in value['changes']):
            raise ValueError('test-maintenance-replacement-not-passed')
        return {'status': 'PASS', 'paths': [change['path'] for change in value['changes']],
                'decision': value, 'revision': qa['revision'], 'discovered': discovery['collected'], 'executed': discovery['executed']}
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'FAIL', 'reason': 'test-maintenance-unverified', 'paths': []}


def review_criterion(record: dict[str, Any], qa: dict[str, Any]) -> dict[str, str] | None:
    proof = evidence(record, qa)
    if proof['status'] != 'PASS':
        return None
    return {'id': 'authorized_test_maintenance', 'criterion':
            'Kullanıcı test bakımını yalnız bu revizyon/komut için yetkilendirdi. Özgün istekteki zorunlu kriterleri '
            'düşürmeden eski kontrolün amacının korunduğunu, yerine geçen testlerin olumlu/ret/sınır davranışlarını '
            'gerçekten doğruladığını ve yeni QA keşif/çalıştırma kanıtını incele. Kriter kaybı veya yetersiz kanıt FAIL: '
            + json.dumps(proof, ensure_ascii=False)}
