#!/usr/bin/env python3
"""Adaptive splitting (G-E10): a lane that exhausts its chain at very hard level is split once into 2-3 sequential sub-tasks.
A sub-task is never split again, cannot leave the parent task's files and calls no helper; user decisions and environment failures are not solved by splitting."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

BOLME_SEVIYELERI = ('cok-zor', 'ultra')
BOLUNMEZ_NEDENLER = ('no-capacity', 'disk-low', 'kurulum-hatasi', 'kritik-dosya-ihlali', 'onay-bekliyor')
ALT_EN_AZ, ALT_EN_COK = 2, 3


def _gorev(plan: dict[str, Any], gid: str) -> dict[str, Any] | None:
    return next((g for g in plan.get('gorevler') or [] if g.get('id') == gid), None)


def bolunebilir(plan: dict[str, Any], gid: str, seviye_ad: str, neden: str) -> tuple[bool, str]:
    g = _gorev(plan, gid)
    if seviye_ad not in BOLME_SEVIYELERI:
        return False, f"seviye {seviye_ad or '-'}: bolme yalniz cok-zor+"
    if g is None:
        return False, 'gorev planda yok'
    if g.get('ust'):
        return False, 'alt gorev yeniden bolunmez'
    if g.get('bolundu'):
        return False, 'gorev zaten bolundu'
    if any((neden or '').startswith(n) for n in BOLUNMEZ_NEDENLER):
        return False, f'neden bolmeyle cozulmez: {neden[:80]}'
    return True, 'bolunebilir'


def bolme_ornegi(plan: dict[str, Any], gid: str) -> dict[str, Any]:
    """Sample sub-plan put in the request; it MUST pass `validate_plan`."""
    g = _gorev(plan, gid) or {}
    return {'is_id': f'{gid}-bol', 'gorevler': [{'id': 'a', 'tanim': '...', 'dosyalar': [(g.get('dosyalar') or ['yol'])[0]],
            'kabul': 'olculebilir kabul kriteri', 'bagimli': [], 'zorluk': 3, 'model': 'ds-pro', 'paralel_grup': 1,
            'tahmin_dk': 20, 'tahmin_usd': 0.5}]}


def bolme_istemi(plan: dict[str, Any], gid: str, neden: str, yurutucu_maddeleri: str = '') -> str:
    g = _gorev(plan, gid) or {}
    ornek = bolme_ornegi(plan, gid)
    madde = f"\nYURUTUCUNUN KENDI MADDE LISTESI (hangi parcalar bitti):\n{yurutucu_maddeleri[:3000]}\n" if yurutucu_maddeleri.strip() else ''
    return (
        "SEN BIR GOREV PLANLAYICISIN. Gercek kaynagi salt-okur incele; tek bir JSON blogu yaz.\n"
        "Asagidaki gorev yurutucu zincirinde tukenene kadar denendi ve bitirilemedi"
        f" (son neden: {neden or 'belirtilmedi'}). Gorevi ALT GOREVLERE BOL: 2-3 kucuk, SIRALI alt gorev; her biri tek oturumda "
        "bitecek kadar kucuk, ilki en temel parca. Worktree'de yarim urun olabilir; alt gorevler mevcut durumu devralir.\n"
        f"KURALLAR: dosyalar YALNIZ su listeden (yeni dosya yok): {', '.join(g.get('dosyalar') or [])}; "
        f"kapsam disi: {', '.join(g.get('kapsam_disi') or []) or '-'}; her alt gorevin kabul kriteri olculebilir olsun.\n"
        f"GOREV: {g.get('tanim', '')}\nKABUL: {g.get('kabul', '')}\n{madde}"
        'Kaynak kimliklerini ve ust kabulunu koru; yeni kapsam, izin veya tavan uretme. '
        'Rutin secimleri assumptions, kritik belirsizligi decisions alaninda bildir. '
        'source_items verirsen ust goreve bagli her maddeyi kendi hash ve alt gorev kimlikleriyle pending olarak esle.\n'
        + 'KAYNAK BAGLARI: ' + json.dumps({'is_turu': plan.get('is_turu'),
          'source_items': [item for item in plan.get('source_items', []) if gid in item['tasks']],
          'source_coverage': plan.get('source_coverage'),
          'assumptions': plan.get('assumptions', []), 'decisions': plan.get('decisions', [])}, ensure_ascii=False) + '\n'
        f"SEMA (ornek, ayni alanlar): {json.dumps(ornek, ensure_ascii=False)}\n"
    )


def alt_plan_birlestir(plan: dict[str, Any], gid: str, ham: str, root: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Validates the planner output and appends it as sequential sub-tasks after the parent task; returns (new plan, sub ids)."""
    from dispatch_util import extract_plan, validate_plan
    from kosu import request_plan_coverage
    from sonuc import merge_uncertainties
    ust = _gorev(plan, gid)
    if ust is None:
        raise ValueError('gorev planda yok')
    alt_plan = extract_plan(ham)
    validate_plan(alt_plan, root)
    if alt_plan.get('is_turu', plan.get('is_turu')) != plan.get('is_turu'):
        raise ValueError('request-job-type-mismatch')
    alt = alt_plan['gorevler']
    if not ALT_EN_AZ <= len(alt) <= ALT_EN_COK:
        raise ValueError(f'alt gorev sayisi {len(alt)}; {ALT_EN_AZ}-{ALT_EN_COK} olmali')
    izinli = set(_normal_dosyalar(ust, root))
    ids = [f'{gid[:29]}-{i}' for i in range(1, len(alt) + 1)]
    yeniler = []
    for i, (kimlik, g) in enumerate(zip(ids, alt)):
        dosyalar = _normal_dosyalar(g, root)
        disari = sorted(set(dosyalar) - izinli)
        if disari:
            raise ValueError(f'alt gorev ust gorevin disinda dosya istiyor: {", ".join(disari[:5])}')
        yeni = {k: v for k, v in g.items() if k not in ('id', 'bagimli')}
        yeniler.append({**yeni, 'id': kimlik, 'dosyalar': dosyalar, 'bagimli': ids[i - 1:i], 'ust': gid})
    sonuc = copy.deepcopy(plan)
    sonuc.update(merge_uncertainties(plan, alt_plan))
    sira = [t['id'] for t in sonuc['gorevler']].index(gid)
    sonuc['gorevler'][sira]['bolundu'] = ids
    sonuc['gorevler'][sira + 1:sira + 1] = yeniler
    sources = [item for item in plan.get('source_items', []) if gid in item['tasks']]
    if 'source_items' in alt_plan:
        if (not isinstance(alt_plan['source_items'], list)
                or any(not isinstance(item, dict) or item.get('status') != 'pending' for item in alt_plan['source_items'])):
            raise ValueError('request-backlog-coverage')
        request_plan_coverage(alt_plan, {'items': [{'id': item['id'], 'sha256': item['source_sha256'], 'truncated': False}
                                                for item in sources]})
        children = {item['id']: item['tasks'] for item in alt_plan['source_items']}
    else:
        children = {item['id']: [task['id'] for task in alt] for item in sources}
    renamed = {task['id']: name for task, name in zip(alt, ids)}
    for item in sonuc.get('source_items', []):
        if gid in item['tasks']:
            item['tasks'] = [*item['tasks'], *[renamed[name] for name in children[item['id']]]]
    if isinstance(sonuc.get('source_coverage'), dict):
        merged = []
        for task in sonuc['gorevler']:
            mapped = [item['id'] for item in sonuc.get('source_items', []) if task['id'] in item['tasks']]
            if len(mapped) > 1:
                merged.append({'task': task['id'], 'source_items': mapped})
        sonuc['source_coverage']['merged'] = merged
    if isinstance(sonuc.get('birlestirme_sirasi'), list) and gid in sonuc['birlestirme_sirasi']:
        j = sonuc['birlestirme_sirasi'].index(gid)
        sonuc['birlestirme_sirasi'][j + 1:j + 1] = ids
    validate_plan(sonuc, root)
    return sonuc, ids


def _normal_dosyalar(g: dict[str, Any], root: str | Path | None) -> list[str]:
    from dispatch_util import plan_path
    return list(dict.fromkeys(plan_path(n, root) for n in g.get('dosyalar') or []))


def _kayit(run_id: str):
    from kosu import RunStore
    store = RunStore()
    return store, store.read(run_id), store.path(run_id) / 'plan.json'


def _neden(store, run_id: str, gid: str) -> str:
    durum = store.task_dir(run_id, gid) / 'status.txt'
    parcalar = durum.read_text(errors='replace').split(None, 1) if durum.exists() else []
    return parcalar[1].strip() if len(parcalar) > 1 else ''


if __name__ == '__main__':
    kom, a = (sys.argv[1] if len(sys.argv) > 1 else ''), sys.argv[2:]
    if kom == 'bolunebilir' and len(a) >= 3:
        store, _k, yol = _kayit(a[0])
        ok, neden = bolunebilir(json.loads(yol.read_text()), a[1], a[2], _neden(store, a[0], a[1]))
        print(neden)
        raise SystemExit(0 if ok else 1)
    if kom == 'istem' and len(a) >= 2:
        store, kayit, yol = _kayit(a[0])
        maddeler = Path(kayit.get('worktree') or '/yok') / '.gorevler.json'
        print(bolme_istemi(json.loads(yol.read_text()), a[1], _neden(store, a[0], a[1]),
                           maddeler.read_text(errors='replace') if maddeler.exists() else ''))
        raise SystemExit(0)
    if kom == 'birlestir' and len(a) >= 3:
        from kosu import automatic_revision
        _store, _k, yol = _kayit(a[0])
        try:
            yeni, ids = alt_plan_birlestir(json.loads(yol.read_text()), a[1], sys.stdin.read(), a[2])
        except ValueError as exc:
            print(f'BOLME: {exc}', file=sys.stderr)
            raise SystemExit(2)
        import os
        review = automatic_revision(_store, a[0], yeni, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
        if review.get('status') != 'PASS':
            print('BOLME: current-plan-review-required', file=sys.stderr)
            raise SystemExit(78)
        print(' '.join(ids))
        raise SystemExit(0)
    if kom == 'alt' and len(a) >= 2:
        _store, _k, yol = _kayit(a[0])
        print(' '.join((_gorev(json.loads(yol.read_text()), a[1]) or {}).get('bolundu') or []))
        raise SystemExit(0)
    print(__doc__)
    raise SystemExit(2)
