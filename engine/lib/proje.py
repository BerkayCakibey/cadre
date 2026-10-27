#!/usr/bin/env python3
"""Project mode (G-P): an ultra-level job is split into a feature list; each feature accumulates as a child run on an intermediate branch
(`cadre/proje-<id>`), the canonical branch is not touched. The first layer is a pure core (no model/network/git)."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

OZELLIK_TAVANI = 40          # cap on pending features (per list)
DENEME_TAVANI = 2            # on the second failure the feature gets stuck, the project continues
ARDISIK_BASARISIZ_TAVAN = 3  # this many failed features in a row = no progress
ILERLEME_YOK_SN = 86400      # stop if no new feature passed within 24 h
SURE_TAVANI_SN = 7 * 86400   # project time horizon
SON_KABUL_TURU = 2
PLAN_ISTEK_TAVANI = 150_000
BELGE_UZANTILARI = ('.md', '.markdown', '.txt', '.rst', '.adoc')
HASSAS_YOL = re.compile(r'(^|/)\.env|secret|credential|passw|token|private|id_rsa|id_ed25519', re.I)
OLUMSUZ = re.compile(r"\b(?:oku|dokun|kullan)ma(?:y[ıi]n)?\b|\basla\b|\bhari[cç]\b"
                     r"|\b(?:do not|don't|never|ignore|exclude|skip)\b", re.I)
EK_DOSYA_TAVANI = 40_000
BAGLAM_TAVANI = 6000
KIMLIK = re.compile(r'[a-z0-9_-]{1,32}')


STEP_POLICY = {'version': 1, 'mode': 'step'}
STEP_INSTRUCTIONS = ('GOZETIM: isler sirayla yurutulur ve her ozellikten sonra operator dogrulamasi beklenir. '
                     'Bir goreve farkli backlog maddelerini birlestirme; her kaynak kimligini ayri gorev/kabul olarak koru. '
                     'Bir kart gerekirse birden cok kucuk goreve bolunebilir; tum kart ancak tam kabulle kapanir.\n')


def supervision_policy(home: Path, project: str) -> dict[str, Any] | None:
    from runtime_sync import regular
    data = regular(home.resolve() / 'projects' / project / 'supervision.json')
    if data is None:
        return None
    try:
        policy = json.loads(data) if len(data) <= 4096 else None
    except (ValueError, UnicodeError):
        policy = None
    if policy != STEP_POLICY or type(policy.get('version')) is not int:
        raise ValueError('project-supervision-policy-invalid')
    return dict(STEP_POLICY)


def supervision_waiting(store: Any, record: dict[str, Any]) -> bool:
    from kosu import bounded_json
    if record.get('supervision') is None:
        return False
    if record['supervision'] != STEP_POLICY or type(record['supervision'].get('version')) is not int:
        raise ValueError('project-supervision-policy-invalid')
    path = store.path(record['run_id']) / 'proje.json'
    expected = record.get('reason') in ('proje-durdu: project-step-verification-required', 'project-step-resume-required')
    if not path.exists():
        if expected or record.get('project_deadline') is not None or path.is_symlink():
            raise ValueError('project-supervision-checkpoint-invalid')
        return False
    try:
        state = bounded_json(path)
    except (OSError, ValueError):
        raise ValueError('project-supervision-checkpoint-invalid') from None
    if (not _yeni_durum(record['run_id'], '', 0).keys() <= state.keys()
            or not isinstance(state['cocuklar'], list) or not isinstance(state['ozellikler'], list)
            or any(not isinstance(f, dict) for f in state['ozellikler'])):
        raise ValueError('project-supervision-checkpoint-invalid')
    checkpoint = state.get('checkpoint')
    if checkpoint is None:
        if expected or state.get('cocuklar') or any(f.get('kosu') for f in state.get('ozellikler', [])):
            raise ValueError('project-supervision-checkpoint-invalid')
        return False
    if (not isinstance(checkpoint, dict) or checkpoint.get('status') not in ('waiting', 'verified', 'retry-approved')
            or not isinstance(checkpoint.get('id'), str) or not isinstance(checkpoint.get('success'), bool)):
        raise ValueError('project-supervision-checkpoint-invalid')
    return checkpoint['status'] == 'waiting'


def verify_step(store: Any, run_id: str, checkpoint_id: str, action: str = 'continue', note: str = '') -> dict[str, Any]:
    """N-RUN01/N-QA05: operator verifies one bound result; spending and the original deadline remain fixed."""
    from kosu import (
        TERMINAL,
        FileLock,
        atomic_json,
        bounded_json,
        mudahale_ekle,
        plan_decisions,
        plan_digest,
        plan_hold,
    )
    from sonuc import approved, implemented
    if os.environ.get('CADRE_RUN_ID') or action not in ('continue', 'retry') or len(note) > 4000:
        raise ValueError('project-step-operator-command-required')
    owner = FileLock(store.path(run_id) / '.owner.lock')
    if not owner.acquire(blocking=False):
        raise ValueError('project-step-owner-active')
    try:
        with FileLock(store.path(run_id) / '.record.lock'):
            record = store.read(run_id)
            if (record.get('supervision') != STEP_POLICY or record['state'] in TERMINAL or plan_hold(record)
                    or plan_decisions(record) or (store.path(run_id) / 'cancel.flag').exists()):
                raise ValueError('project-step-release-blocked')
            supervision_waiting(store, record)
            path = store.path(run_id) / 'proje.json'
            state = bounded_json(path)
            checkpoint = state.get('checkpoint') or {}
            if checkpoint.get('id') != checkpoint_id:
                raise ValueError('project-step-stale-checkpoint')
            if checkpoint.get('status') != 'waiting':
                if checkpoint.get('status') == ('verified' if action == 'continue' else 'retry-approved'):
                    return checkpoint
                raise ValueError('project-step-release-blocked')
            child = store.read(checkpoint['run_id'])
            if (child.get('proje_ust') != run_id or checkpoint.get('final_digest') != plan_digest(child.get('final_result') or {})):
                raise ValueError('project-step-result-changed')
            if action == 'continue':
                if not checkpoint.get('success') or not (approved(child) or implemented(child)):
                    raise ValueError('project-step-acceptance-required')
                import sqlite3
                from contextlib import closing
                ledger = store.home / 'zincir/request-budget.db'
                if ledger.is_symlink() or ledger.parent.is_symlink():
                    raise ValueError('project-step-unsettled-cost')
                if ledger.exists():
                    with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as con:
                        pending = con.execute("select count(*) from reservation where actual is null "
                                              "and json_extract(metadata,'$.run_id')=? "
                                              "and coalesce(json_extract(metadata,'$.guvence'),0)!=1", (child['run_id'],)).fetchone()[0]
                    if pending:
                        raise ValueError('project-step-unsettled-cost')
            elif checkpoint.get('success') or not note.strip():
                raise ValueError('project-step-retry-evidence-required')
            elif checkpoint['feature'] != 'son-kabul':
                feature = next((f for f in state['ozellikler'] if f['id'] == checkpoint['feature']), None)
                if feature is None or feature['durum'] != 'bekliyor':
                    raise ValueError('project-plan-review-required')
                state['retry_feature'] = feature['id']
            checkpoint = {**checkpoint, 'status': 'verified' if action == 'continue' else 'retry-approved',
                          'verified_at': time.time(), 'note': note}
            state['checkpoint'] = checkpoint
            mudahale_ekle(record, 'project-step-verify', {'checkpoint': checkpoint_id, 'action': action,
                                                       'child': child['run_id'], 'feature': checkpoint['feature'], 'note': note})
            record.update(state='accepted', reason='project-step-resume-required', owner_pid=None, updated=time.time())
            atomic_json(store.path(run_id) / 'run.json', record)
            atomic_json(path, state)
            return checkpoint
    finally:
        owner.close()


def _ham_plan(ham: str) -> dict[str, Any]:
    """Returns the single, unambiguous project plan in the text."""
    cozucu = json.JSONDecoder()
    from sonuc import validate_uncertainties
    bulunan = []
    i = ham.find('{')
    while i != -1:
        try:
            deger, son = cozucu.raw_decode(ham, i)
        except ValueError:
            i = ham.find('{', i + 1)
            continue
        if isinstance(deger, dict) and 'ozellikler' in deger:
            validate_uncertainties(deger)
            if not isinstance(deger['ozellikler'], list):
                raise ValueError('ozellik listesi (ozellikler) bulunamadi')
            bulunan.append(deger)
        i = ham.find('{', son)
    if not bulunan:
        raise ValueError('ozellik listesi (ozellikler) bulunamadi')
    if len(bulunan) != 1:
        raise ValueError('one unambiguous valid project plan required')
    return bulunan[0]


def _ham_liste(ham: str) -> list[Any]:
    return _ham_plan(ham)['ozellikler']


def _dogrula(liste: list[Any], bilinen: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    if not isinstance(liste, list) or not liste:
        raise ValueError('ozellik listesi bos')
    if len(liste) > OZELLIK_TAVANI:
        raise ValueError(f'ozellik sayisi {len(liste)} > {OZELLIK_TAVANI}')
    temiz: list[dict[str, Any]] = []
    for f in liste:
        if not isinstance(f, dict) or not isinstance(f.get('id'), str) or not KIMLIK.fullmatch(f['id']):
            raise ValueError('gecersiz ozellik kimligi')
        for alan in ('baslik', 'aciklama', 'kabul'):
            if not isinstance(f.get(alan), str) or not f[alan].strip() or len(f[alan]) > 8000:
                raise ValueError(f"{f['id']}: {alan} bos ya da gecersiz")
        oncelik = f.get('oncelik', 3)
        if type(oncelik) is not int or not 1 <= oncelik <= 5:
            raise ValueError(f"{f['id']}: oncelik 1-5 olmali")
        bagimli = f.get('bagimli', [])
        if not isinstance(bagimli, list) or any(not isinstance(b, str) for b in bagimli):
            raise ValueError(f"{f['id']}: bagimli liste olmali")
        if not _kabul_komutu(f) or len(f['qa_komut']) > 2000:
            raise ValueError(f"{f['id']}: qa_komut bos ya da gecersiz")
        temiz.append({'id': f['id'], 'baslik': f['baslik'].strip(), 'aciklama': f['aciklama'].strip(), 'kabul': f['kabul'].strip(),
                      'bagimli': list(dict.fromkeys(bagimli)), 'oncelik': oncelik, 'durum': 'bekliyor', 'deneme': 0, 'kosu': [],
                      'qa_komut': _kabul_komutu(f)})
        for key in ('kapsam_puani', 'tahmin_usd', 'riskli', 'repair_of', 'source_tasks', 'source_items', 'source_acceptance'):
            if key in f:
                temiz[-1][key] = f[key]
        if 'repair_of' in f and (not isinstance(f['repair_of'], str) or not KIMLIK.fullmatch(f['repair_of']) or f['repair_of'] == f['id']):
            raise ValueError('gecersiz onarim kaynagi')
        ozellik_tavani(temiz[-1])
    kimlikler = [f['id'] for f in temiz]
    if len(set(kimlikler)) != len(kimlikler) or set(kimlikler) & bilinen:
        raise ValueError('tekrarlanan ozellik kimligi')
    tum = set(kimlikler) | bilinen
    for f in temiz:
        if any(b not in tum or b == f['id'] for b in f['bagimli']):
            raise ValueError(f"{f['id']}: bilinmeyen bagimlilik")
    _dongu_yok(temiz)
    return temiz


def _dongu_yok(liste: list[dict[str, Any]]) -> None:
    bag = {f['id']: set(f['bagimli']) for f in liste}
    biten: set[str] = set()
    while bag:
        hazir = [k for k, v in bag.items() if v <= biten | (v - set(bag))]
        if not hazir:
            raise ValueError('ozellik bagimliliklari dongusel')
        for k in hazir:
            biten.add(k)
            del bag[k]


def ozellik_listesi(ham: str) -> list[dict[str, Any]]:
    return _dogrula(_ham_liste(ham))


def siradaki(liste: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The highest-priority (smallest number) pending feature whose dependencies have passed; list order on a tie."""
    gecen = {f['id'] for f in liste if f['durum'] in ('gecti', 'uygulandi')}
    adaylar = [(f['oncelik'], i, f) for i, f in enumerate(liste) if f['durum'] == 'bekliyor' and set(f['bagimli']) <= gecen]
    return min(adaylar, key=lambda a: (a[0], a[1]))[2] if adaylar else None


def sonuc_isle(f: dict[str, Any], kosu_id: str, basari: bool) -> dict[str, Any]:
    yeni = {**f, 'deneme': f['deneme'] + 1, 'kosu': [*f['kosu'], kosu_id]}
    yeni['durum'] = 'uygulandi' if basari else ('takildi' if yeni['deneme'] >= DENEME_TAVANI else 'bekliyor')
    return yeni


def dur_karari(durum: dict[str, Any], simdi: float) -> tuple[bool, str]:
    """(stop?, reason). `(False, 'son-kabul')` = features done, move to whole-project acceptance."""
    liste = durum['ozellikler']
    if simdi - durum['baslangic'] >= SURE_TAVANI_SN:
        return True, 'sure-tavani'
    kalan = [f for f in liste if f['durum'] == 'bekliyor']
    if not kalan:
        if not any(f['durum'] in ('gecti', 'uygulandi') for f in liste):
            return True, 'ilerleme-yok'
        if durum.get('son_kabul_turu', 0) >= SON_KABUL_TURU:
            return True, 'son-kabul-turlari-bitti'
        kanitsiz = [f['id'] for f in liste if f['durum'] == 'takildi' and not _kabul_komutu(f)]
        if kanitsiz:
            return True, 'kanitsiz-takilan-ozellik: ' + ','.join(kanitsiz)
        return False, 'son-kabul'
    if simdi - durum['baslangic'] >= SURE_TAVANI_SN:
        return True, 'sure-tavani'
    if durum.get('ardisik_basarisiz', 0) >= ARDISIK_BASARISIZ_TAVAN:
        return True, 'ilerleme-yok'
    if simdi - max(durum.get('son_gecti') or 0.0, durum['baslangic']) >= ILERLEME_YOK_SN:
        return True, 'ilerleme-yok'
    if siradaki(liste) is None:
        return True, 'bloke'
    return False, ''


def _amac(metin: str) -> set[str]:
    import unicodedata
    metin = unicodedata.normalize('NFKD', metin.casefold().replace('ı', 'i'))
    metin = ''.join(c for c in metin if not unicodedata.combining(c))
    es = {
        'tespit': 'bul', 'kesfet': 'bul', 'belirle': 'bul', 'bul': 'bul',
        'kaynak': 'kaynak', 'dosya': 'kaynak', 'envanter': 'envanter', 'listele': 'envanter',
        'gorev': 'madde', 'madde': 'madde', 'cikar': 'uret', 'olustur': 'uret', 'yaz': 'uret',
        'dogrula': 'kontrol', 'denetle': 'kontrol', 'kontrol': 'kontrol',
    }
    gereksiz = {'aktif', 'tum', 'butun', 'bir', 've', 'ile', 'icin', 'et', 'yap', 'mevcut'}
    sonuc = set()
    for kelime in re.findall(r'[a-z0-9_]+', metin):
        if kelime in gereksiz:
            continue
        kok = next((v for k, v in es.items() if kelime.startswith(k)), kelime)
        sonuc.add(kok)
    return sonuc


def ayni_amac(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """B315: renaming the same goal is not a new feature."""
    for alan in ('baslik', 'kabul'):
        x, y = _amac(a.get(alan, '')), _amac(b.get(alan, ''))
        minimum = 4 if alan == 'kabul' else 2
        if len(x) < minimum or len(y) < minimum:
            continue
        kod_x = {v for v in x if re.fullmatch(r'[a-z]+[0-9]+', v)}
        kod_y = {v for v in y if re.fullmatch(r'[a-z]+[0-9]+', v)}
        if kod_x and kod_y and kod_x.isdisjoint(kod_y):
            continue
        if len(x & y) / len(x | y) >= .8:
            return True
    return False


def yeniden_plan_birlestir(eski: list[dict[str, Any]], ham: str) -> list[dict[str, Any]]:
    """Passed and stuck features are kept (with their history); pending ones change with the new list (the attempt history of the same id is carried)."""
    korunan = [copy.deepcopy(f) for f in eski if f['durum'] in ('gecti', 'uygulandi', 'takildi')]
    korunan_ids = frozenset(f['id'] for f in korunan)
    gecmis = {f['id']: f for f in eski if f['durum'] == 'bekliyor'}
    yeni = _dogrula([f for f in _ham_liste(ham) if not (isinstance(f, dict) and f.get('id') in korunan_ids)], korunan_ids)
    takilan = {f['id'] for f in korunan if f['durum'] == 'takildi'}
    for f in yeni:
        if set(f['bagimli']) & takilan:
            raise ValueError(f"{f['id']}: tamamlanmamis korunan bagimlilik")
        if f.get('repair_of'):
            kaynak = next((g for g in korunan if g['id'] == f['repair_of']), None)
            if kaynak is None or f['kabul'] != kaynak['kabul']:
                raise ValueError(f"{f['id']}: onarim kaynak kabulunu korumali")
    for i, f in enumerate(yeni):
        onceki = next((g for g in [*korunan, *yeni[:i]] if ayni_amac(f, g)
                       and not (f.get('repair_of') == g['id'] and f['kabul'] == g['kabul'])), None)
        if onceki:
            raise ValueError(f"{f['id']}: yinelenen amac ({onceki['id']}); mevcut ozelligin kabulunu tekrar etme")
    for f in yeni:
        if f['id'] in gecmis:
            f['deneme'], f['kosu'] = gecmis[f['id']]['deneme'], list(gecmis[f['id']]['kosu'])
    return korunan + yeni


def proje_kaynaklari(plan: dict[str, Any], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks = {task['id']: task for task in plan.get('gorevler', [])}
    if not tasks:
        if plan.get('source_items') or any(f.get('source_tasks') or f.get('source_items') or f.get('source_acceptance') for f in features):
            raise ValueError('project-source-coverage')
        return copy.deepcopy(features)
    covered: set[str] = set()
    result = []
    for feature in features:
        names = feature.get('source_tasks')
        if (not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in tasks for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('project-source-coverage')
        covered.update(names)
        items = [{k: item[k] for k in ('id', 'source_sha256', 'evidence')}
                 for item in plan.get('source_items', []) if set(names) & set(item['tasks'])]
        acceptance = [{'id': name, 'kabul': tasks[name]['kabul']} for name in names]
        for key, value in (('source_items', items), ('source_acceptance', acceptance)):
            if key in feature and feature[key] != value:
                raise ValueError('project-source-coverage')
        result.append({**copy.deepcopy(feature), 'source_items': items, 'source_acceptance': acceptance})
    if covered != tasks.keys():
        raise ValueError('project-source-coverage')
    return result


def proje_plani_kaydet(store: Any, run_id: str, features: list[dict[str, Any]], raw: str = '') -> list[dict[str, Any]]:
    if not hasattr(store, 'home'):
        return features
    from kosu import automatic_revision, bounded_json, plan_digest
    from sonuc import merge_uncertainties
    import os
    path = store.path(run_id) / 'plan.json'
    plan = bounded_json(path) if path.exists() else {'is_id': run_id, 'kind': 'project', 'gorevler': []}
    before = plan_digest(plan)
    if raw:
        envelope = _ham_plan(raw)
        if envelope.get('is_turu', plan.get('is_turu')) != plan.get('is_turu'):
            raise ValueError('request-job-type-mismatch')
        plan.update(merge_uncertainties(plan, envelope))
    features = proje_kaynaklari(plan, features)
    if store.read(run_id).get('supervision') == STEP_POLICY and any(len(f.get('source_items', [])) > 1 for f in features):
        raise ValueError('project-step-multiple-source-items')
    definitions = [{k: v for k, v in f.items() if k not in ('durum', 'deneme', 'kosu', 'son_kabulde')} for f in features]
    history = store.plan_view(run_id)['history']
    original = next((e['plan']['project_features'] for e in history if e['plan'].get('project_features')), definitions)
    scope = {f['id']: f for f in original if not f.get('repair_of')}
    if not set(scope) <= {f['id'] for f in definitions}:
        raise ValueError('project-scope-removal')
    for f in definitions:
        source = scope.get(f.get('repair_of') or f['id'])
        if (not source or f['kabul'] != source['kabul'] or f.get('source_tasks') != source.get('source_tasks')
                or (not f.get('repair_of') and f['baslik'] != source['baslik'])):
            raise ValueError('project-scope-expansion')
    plan['project_features'] = definitions
    unchanged = before == plan_digest(plan)
    if not path.exists():
        store.save_plan(run_id, plan)
        from kosu import plan_ready
        review = plan_ready(store, run_id, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
    elif unchanged and not store.plan_view(run_id)['held']:
        from kosu import plan_ready
        review = plan_ready(store, run_id, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
    else:
        review = automatic_revision(store, run_id, plan, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
    if review.get('status') != 'PASS':
        raise ValueError('current-plan-review-required')
    return features


def proje_butcesi_sabitle(store: Any, run_id: str, started: float | None = None) -> None:
    from kosu import FileLock, atomic_json
    from butce_kapisi import amount
    if not hasattr(store, 'home'):
        return
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        cap = amount(r.get('operator_budget_usd', r.get('budget_cap_usd')))
        if 'project_budget_cap_usd' in r:
            cap = min(cap, amount(r['project_budget_cap_usd']))
        saved = r.get('project_operator_budget')
        if saved:
            cap = min(cap, amount(saved['amount_usd']))
        r.update(project_budget_cap_usd=cap, project_operator_budget={
            'amount_usd': cap, 'currency': 'USD', 'scope': 'operator-total',
            'providers': ['opencode', 'openrouter'],
            'subscription_usage': {'opencode-go': {'cap_usd_equivalent': cap}, 'claude': {'cap_usd_equivalent': cap}}}, project_run_ids=sorted(set(r.get('project_run_ids') or []) | {run_id}))
        if started is not None:
            r['project_deadline'] = min(r.get('project_deadline', started + SURE_TAVANI_SN), started + SURE_TAVANI_SN)
        atomic_json(store.path(run_id) / 'run.json', r)


def ozellik_tavani(f: dict[str, Any]) -> float:
    from seviye import plan_tavani, seviye
    puan = f.get('kapsam_puani', 50)
    if type(puan) is not int or not 0 <= puan <= 100:
        raise ValueError('ozellik kapsam puani 0-100 olmali')
    return plan_tavani({'gorevler': [{'tahmin_usd': f.get('tahmin_usd', 0)}]}, seviye(min(puan, 85))['tavan_usd'])


def proje_tavani_yaz(store: Any, run_id: str, liste: list[dict[str, Any]]) -> None:
    from contextlib import nullcontext
    from kosu import FileLock, atomic_json
    proje_butcesi_sabitle(store, run_id)
    with FileLock(store.path(run_id) / '.record.lock') if hasattr(store, 'home') else nullcontext():
        r = store.read(run_id)
        plan = {f['id']: max(ozellik_tavani(f), (r.get('project_feature_caps') or {}).get(f['id'], 0)) for f in liste}
        ids = set(r.get('project_run_ids') or [run_id])
        if hasattr(store, 'home'):
            ids.update(c['run_id'] for c in store.list() if c.get('proje_ust') == run_id)
        r.update(project_budget_plan=plan, project_run_ids=sorted(ids))
        if hasattr(store, 'home'):
            atomic_json(store.path(run_id) / 'run.json', r)
        else:
            store.update(run_id, **{k: r[k] for k in ('project_budget_plan', 'project_run_ids')})


def ozellik_tavani_guncelle(store: Any, run_id: str, feature: str, cap: float) -> None:
    """P2: the child's detailed plan updates the preliminary estimate in the project list."""
    from kosu import FileLock, atomic_json
    from butce_kapisi import amount
    cap = amount(cap)
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        plan = r.get('project_budget_plan') or {}
        if feature not in plan:
            return
        r.setdefault('project_feature_caps', {})[feature] = cap
        plan[feature] = max(plan[feature], cap)
        atomic_json(store.path(run_id) / 'run.json', r)


def proje_maliyeti(home: str | Path, kosular: list[str]) -> dict[str, float] | None:
    """B291: ledger total of the main + child runs; measured receipts, ghost estimates (B85) and pending separately. None without a ledger."""
    import sqlite3
    from contextlib import closing
    from butce_kapisi import USD_PROVIDERS
    db = Path(home or '/nonexistent') / 'zincir/request-budget.db'
    if not home or not db.exists() or not kosular:
        return None
    try:
        from kosu import RunStore
        ids = set(kosular)
        # G-B12/B317: an active/cancelled child not yet added to the parent summary is also included in the ledger.
        ids.update(r['run_id'] for r in RunStore(Path(home)).list() if r.get('proje_ust') in kosular)
        kosular = sorted(ids)
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            satirlar = con.execute(
                "select reserved, actual, receipt, metadata from reservation where json_extract(metadata,'$.run_id') in (%s) and provider in (%s)"
                % (','.join('?' * len(kosular)), ','.join('?' * len(USD_PROVIDERS))), (*kosular, *USD_PROVIDERS)).fetchall()
    except (OSError, ValueError, sqlite3.Error):
        return None
    from butce_kapisi import kurtarma_tutari, BudgetDenied
    tahmin = ('hayalet:', 'kurtarma:')
    olculen = sum(a for _, a, r, _ in satirlar if a is not None and not str(r or '').startswith(tahmin))
    tahmini = sum(a for _, a, r, _ in satirlar if a is not None and str(r or '').startswith(tahmin))
    askida = 0
    for rz, a, _, meta in satirlar:
        if a is None:
            try:
                askida += kurtarma_tutari(rz, json.loads(meta or '{}'))
            except (ValueError, TypeError, AttributeError, BudgetDenied):
                return None
    return {k: round(v / 1e6, 6) for k, v in (('olculen_usd', olculen), ('tahmini_usd', tahmini), ('askida_usd', askida),
                                             ('yukumluluk_usd', olculen + tahmini + askida))}


def hesap_ozeti(maliyet: dict[str, float], tavan_usd: float) -> dict[str, float]:
    return {**maliyet, 'tavan_usd': round(float(tavan_usd), 6),
            'kalan_usd': round(float(tavan_usd) - maliyet['yukumluluk_usd'], 6)}


def _harcama_satiri(d: dict[str, Any]) -> str:
    m = d.get('maliyet')
    if not isinstance(m, dict):
        return f"harcama ${float(d.get('harcama_usd') or 0):.2f} (cocuk ozetleri)"
    return (f"kesin ${m['olculen_usd']:.2f} · tahmini ${m['tahmini_usd']:.2f} · askida ust sinir ${m['askida_usd']:.2f}"
            f" · yukumluluk ${m['yukumluluk_usd']:.2f}")


def rapor(d: dict[str, Any]) -> str:
    liste = d.get('ozellikler') or []
    say = {k: sum(1 for f in liste if f['durum'] == k) for k in ('gecti', 'uygulandi', 'takildi', 'bekliyor')}
    satirlar = [f"# Proje raporu — {d.get('kimlik', '?')}", '',
                f"İstek: {d.get('istek_ozeti', '')}",
                f"Durum: {d.get('durum', '?')}" + (f" ({d['neden']})" if d.get('neden') else '') +
                f" · ara dal `{d.get('ara_dal', '-')}` · " + _harcama_satiri(d),
                f"Özellikler: {say['gecti']} gecti · {say['uygulandi']} uygulandi · {say['takildi']} takildi · {say['bekliyor']} bekliyor", '',
                '| id | başlık | durum | deneme | koşular |', '|---|---|---|---|---|']
    satirlar += [f"| {f['id']} | {f['baslik']} | {f['durum']}{' (son kabulde)' if f.get('son_kabulde') else ''} | {f['deneme']} | "
                 f"{', '.join(f['kosu'][-3:]) or '-'} |" for f in liste]
    return '\n'.join(satirlar) + '\n'


# --- Orchestration (G-P1..P6): on top of the pure core; the planner/child run/merge are supplied from outside ---

from seviye import BAGLAM_ISARETI  # noqa: E402  the context part of the child prompt does not enter the rule floor (G-P1)

LISTE_SEMASI = {'ozellikler': [{'id': 'f1', 'baslik': '...', 'aciklama': '...', 'kabul': 'olculebilir kabul (hangi test/komut)',
                                'bagimli': [], 'oncelik': 1, 'kapsam_puani': 50, 'tahmin_usd': 2.0, 'qa_komut': 'hedefli test komutu', 'riskli': False}]}


def _kabul_komutu(f: dict[str, Any]) -> str:
    komut = f.get('qa_komut')
    return komut.strip() if isinstance(komut, str) and komut.strip() and komut.strip() != 'hedefli test komutu' else ''


def _ekler_blogu(ekler: str) -> str:
    return f"ISTEKTE ADI GECEN DOSYALAR (kabul anindaki icerik):\n{ekler}\n" if ekler else ''


def liste_istemi(istek: str, harita: str = '', ekler: str = '', ilk_plan: str = '') -> str:
    # B346: in an existing repo (map filled) skeleton/preparation does not count as product progress; features are bound to the acceptance of the items in the source.
    yapi = ("Ilk ozellik iskelet olsun (proje yapisi, test altyapisi, kalite komutu). "
            if not harita else
            "Mevcut repo isinde ayri bir iskelet/hazirlik ozelligi acma; her ozellik istekteki somut bir maddenin (backlog) "
            "kabul kriterini karsilasin. Arastirma/kesif/envanter gibi hazirlik isleri ozellik degildir. ")
    return ("SEN BIR PROJE PLANLAYICISIN. Gercek kaynak dosyalarini salt-okur araclarla incele; SADECE tek bir JSON blogu yaz.\n"
            f"Asagidaki istegi, her biri TEK bir gelistirme kosusuna (1-4 saat) sigan, kabul kriteri olculebilir OZELLIKLERE bol "
            f"(hedef 6 ozellik; gerektiginde daha fazla, guvenlik tavani {OZELLIK_TAVANI}). {yapi}"
            "Istekte ve adi gecen dosyalarda yazan her zorunlu maddeyi bir ozellige bagla; hicbirini dusurme. "
            "Bagimliliklari yaz; oncelik 1 (en once) - 5. Kimlikler kucuk harf (f1, f2, ...).\n"
            "Her ozellik icin hizli ve hedefli kabul komutunu qa_komut alanina yaz; tum test takimini yazma. "
            "Auth, sir, izin, ag veya veri kaybi riski varsa riskli=true yaz (ara hakem gerektirir). "
            "Her ozellik icin kapsam puani (0-100) ve tahmini toplam dolar maliyetini (tahmin_usd) yaz.\n"
            "Ilk plandaki gorevleri her ozelligin source_tasks listesine bagla; tum gorev kimlikleri kapsansin. "
            "Kaynak kimlik/hash ve ozgun kabul metnini koru. Rutin secimleri assumptions, kritik sorulari decisions olarak bildir.\n"
            f"SEMA: {json.dumps(LISTE_SEMASI, ensure_ascii=False)}\n"
            + (f"REPO HARITASI:\n{harita}\n" if harita else '') + f"ISTEK:\n{istek}\n" + _ekler_blogu(ekler)
            + (f"ILK PLANLAYICININ CIKARDIGI GOREVLER (ipucu; kapsami kaybetme):\n{ilk_plan}\n" if ilk_plan else ''))


def ozellik_istemi(istek: str, d: dict[str, Any], f: dict[str, Any], yol: str = '') -> str:
    biten = [g for g in d['ozellikler'] if g['durum'] in ('gecti', 'uygulandi')]
    baglam = istek if len(istek) <= BAGLAM_TAVANI else (
        istek[:BAGLAM_TAVANI] + f"\n[... istegin tamami {len(istek)} karakter: {yol or 'ana kosunun prompt.md dosyasi'}]")
    return (f"PROJE KIPI — ozellik {f['id']}: {f['baslik']} ({len(biten) + 1}/{len(d['ozellikler'])})\n"
            f"Bu kosuda YALNIZ bu ozelligi yap: {f['aciklama']}\nKABUL: {f['kabul']}\n"
            + ('KAYNAK VE OZGUN KABULLER: ' + json.dumps({k: f.get(k, []) for k in ('source_tasks', 'source_items', 'source_acceptance')}, ensure_ascii=False) + '\n')
            + f"Biten ozellikler (bozma): {', '.join(g['id'] + ' ' + g['baslik'] for g in biten) or '-'}\n"
            "Kurallar: diger ozelliklere gecme; mevcut testleri yesil tut; yeni davranisa test yaz.\n"
            f"{BAGLAM_ISARETI}:\n{baglam}\n")


def son_kabul_istemi(istek: str, d: dict[str, Any], ekler: str = '') -> str:
    takilan = [f for f in d['ozellikler'] if f['durum'] == 'takildi']
    eksik = ''.join(f"- {f['id']} {f['baslik']}: {f['aciklama']}\n  KABUL: {f['kabul']}\n"
                    + (f"  KABUL KOMUTU (son kabul QA'si bunu da kosar): {_kabul_komutu(f)}\n" if _kabul_komutu(f) else '')
                    for f in takilan)
    return ("PROJE SON KABUL — ozellik turu bitti. Projeyi ozgun istege gore bastan sona denetle: eksik ya da bozuk olani duzelt, "
            "tum testler ve kalite komutu yesil olsun; yeni ozellik icat etme.\n"
            f"Biten ozellikler: {', '.join(g['id'] + ' ' + g['baslik'] for g in d['ozellikler'] if g['durum'] in ('gecti', 'uygulandi'))}\n"
            + (f"TAKILAN OZELLIKLER (henuz yapilmadi; bu kosuda tamamla, kabulleri saglanmadan is bitmis sayilmaz):\n{eksik}" if eksik else '')
            + f"OZGUN ISTEK:\n{istek}\n" + _ekler_blogu(ekler))


def yeniden_plan_istemi(istek: str, d: dict[str, Any], son_kabul: dict[str, Any] | None = None, ekler: str = '') -> str:
    # B346: show the description and acceptance text of completed/failed features too; new features must not drift from the acceptance in the source.
    durum = []
    for f in d['ozellikler']:
        o = {k: f[k] for k in ('id', 'baslik', 'durum', 'deneme', 'bagimli', 'oncelik')}
        if f['durum'] in ('gecti', 'uygulandi', 'takildi'):
            o.update(aciklama=f.get('aciklama', ''), kabul=f.get('kabul', ''))
        durum.append(o)
    ek = ''
    if son_kabul is not None:
        ek = ("SON KABUL DUSTU; sonucu (hakem/QA): " + json.dumps(son_kabul.get('final_result') or {}, ensure_ascii=False)[:4000] +
              "\nBu eksikleri kapatan DUZELTME ozellikleri ekle.\n")
    return ("SEN BIR PROJE PLANLAYICISIN. Gercek kaynak dosyalarini salt-okur araclarla incele; SADECE tek bir JSON blogu yaz.\n"
            "Ozellik listesini guncelle: 'gecti' ve 'takildi' olanlar degismez (yazmasan da korunur); bekleyenleri bolebilir, "
            "siralayabilirsin; secilen kimlikleri, basliklari ve kabulleri koru. Yeni urun amaci veya ozellik EKLEME. "
              "Onarim gerekiyorsa repair_of ile secili ozellige bagla ve onun kabul metnini aynen koru. "
              "source_tasks kaynak gorev baglarini koru; rutin secimleri assumptions, kritik sorulari decisions olarak bildir.\n" + ek +
            f"MEVCUT DURUM: {json.dumps(durum, ensure_ascii=False)}\n"
            f"SEMA: {json.dumps(LISTE_SEMASI, ensure_ascii=False)}\nOZGUN ISTEK:\n{istek}\n" + _ekler_blogu(ekler))


def ilk_plan_ozeti(kok: Path) -> str:
    try:
        plan = json.loads((Path(kok) / 'plan.json').read_text())
        gorevler = [{k: g[k] for k in ('id', 'tanim', 'kabul', 'dosyalar') if g.get(k)} for g in plan.get('gorevler') or []]
    except (OSError, ValueError, AttributeError, TypeError):
        return ''
    if not gorevler:
        return ''
    return json.dumps({'kapsam_gerekce': plan.get('kapsam_gerekce'), 'gorevler': gorevler,
                       **{key: plan.get(key, []) for key in ('source_items', 'assumptions', 'decisions')},
                       'is_turu': plan.get('is_turu')}, ensure_ascii=False)


def _yeni_durum(run_id: str, istek: str, simdi: float) -> dict[str, Any]:
    kimlik = re.sub(r'[^a-z0-9]', '', run_id.rsplit('-', 1)[-1].lower())[:8] or 'proje'
    ilk = next((satir.strip() for satir in istek.splitlines() if satir.strip()), '')
    return {'kimlik': kimlik, 'istek_ozeti': ilk[:200], 'ara_dal': f'cadre/proje-{kimlik}', 'ozellikler': [],
            'baslangic': simdi, 'son_gecti': simdi, 'ardisik_basarisiz': 0, 'son_kabul_turu': 0,
            'durum': 'calisiyor', 'neden': '', 'harcama_usd': 0.0, 'bitirilen': 0, 'cocuklar': []}


def giris_engeli(kayit: dict[str, Any]) -> int | None:
    from kosu import TERMINAL, plan_hold, git_izin_bekliyor, project_time_expired
    state = kayit.get('state')
    if state in TERMINAL:
        from sonuc import approved
        return 0 if state == 'completed' and approved(kayit) else 130 if state == 'cancelled' else 78
    if plan_hold(kayit):
        return 78
    if _alarm_bekliyor(kayit):
        return 75
    if (git_izin_bekliyor(kayit) or project_time_expired(kayit)
            or kayit.get('reason') == 'proje-durdu: project-plan-review-required'):
        return 78
    return None


def durum_gorunumu(kayit: dict[str, Any], proje: dict[str, Any]) -> dict[str, Any]:
    from kosu import plan_hold
    d = dict(proje)
    state = kayit.get('state')
    reason = str(kayit.get('reason') or '')
    project_reason = reason.removeprefix('proje-durdu: ')
    phase = ''
    if state in ('cancelled', 'failed', 'implemented'):
        d.update(durum='durdu', neden='iptal' if state == 'cancelled' else reason or str(state))
    elif state == 'completed' and d.get('durum') != 'tamam':
        phase = 'Durum çelişkisi'
    elif plan_hold(kayit):
        change = kayit['plan_changes'][-1]
        status = change['status']
        phase = ('Duruş teyidi bekleniyor' if status == 'stop_requested' else
                 'Plan incelemesi' if status == 'review_pending' else
                 'Plan incelemesi reddedildi' if status == 'rejected' else
                 'İzinli devam bekleniyor' if status == 'withdrawn' else 'Plan değişikliği bekleniyor')
        if status != 'stop_requested':
            d.update(durum='durdu', neden=reason)
    elif (proje.get('checkpoint') or {}).get('status') == 'waiting':
        d.update(durum='durdu', neden='project-step-verification-required')
        phase = 'İş sonucu doğrulaması bekleniyor'
    elif state != 'running' and (reason == 'plan-change-resume-recorded' or
            (proje.get('neden') == 'harcama-alarmi' and kayit.get('harcama_onayi_ts') and not _alarm_bekliyor(kayit))):
        d.update(durum='durdu')
        phase = 'İzinli devam bekleniyor'
    elif state == 'retryable':
        d.update(durum='durdu', neden=project_reason or d.get('neden') or '')
    d['asama'] = phase
    return d


def durum_esle(store: Any, run_id: str) -> None:
    from kosu import atomic_json, atomic_write, bounded_json
    path = store.path(run_id) / 'proje.json'
    if not path.exists():
        return
    old = bounded_json(path)
    current = durum_gorunumu(store.read(run_id), old)
    if current != old:
        atomic_json(path, current)
        atomic_write(path.with_name('proje-raporu.md'), rapor(current))


def yurut(store: Any, run_id: str, *, planla: Callable[[str, str], str], cocuk_calistir: Callable[[str, str], dict[str, Any]],
          birlestir: Callable[[str, dict[str, Any]], bool], simdi: Callable[[], float] = time.time, harita: str = '',
          ekler: str = '') -> int:
    """Project loop (single writer, sequential). State in `proje.json`, report in `proje-raporu.md`; resumes where it left off after an interruption."""
    record = store.read(run_id)
    blocked = giris_engeli(record)
    if blocked is not None:
        return blocked
    if supervision_waiting(store, record):
        return 78
    supervised = record.get('supervision') == STEP_POLICY
    kok = Path(store.path(run_id))
    yol = kok / 'proje.json'
    istek_yolu = store.read(run_id)['prompt']
    istek = Path(istek_yolu).read_text(errors='replace')
    d = {**_yeni_durum(run_id, istek, simdi()), **(json.loads(yol.read_text()) if yol.exists() else {})}   # an old record gets the new fields with defaults
    if not d['ozellikler'] and hasattr(store, 'home'):
        view = store.plan_view(run_id)
        review = view['reviews'][-1] if view['reviews'] else {}
        if (view['revision'] and view['execution_plan_matches'] and not view['held'] and review.get('status') == 'PASS'
                and review.get('revision_number') == view['revision']):
            definitions = view['history'][-1]['plan'].get('project_features')
            if definitions:
                d['ozellikler'] = _dogrula(definitions)
    proje_butcesi_sabitle(store, run_id, d['baslangic'])

    def kaydet() -> None:
        from kosu import atomic_json, atomic_write
        d['maliyet'] = proje_maliyeti(getattr(store, 'home', ''), [run_id, *(c.get('kosu') for c in d['cocuklar'] if c.get('kosu'))])
        d['tavan_usd'] = store.read(run_id).get('project_budget_cap_usd')
        atomic_json(yol, d)
        atomic_write(kok / 'proje-raporu.md', rapor(d))

    def cocuk_isle(k: dict[str, Any], etiket: str) -> bool:
        basari = k.get('state') in ('completed', 'implemented') and birlestir(d['ara_dal'], k)
        d['harcama_usd'] = round(d['harcama_usd'] + float(k.get('budget_spent_usd') or 0), 4)
        d['cocuklar'].append({'ozellik': etiket, 'kosu': k.get('run_id'), 'durum': k.get('state'), 'basari': basari,
                              'neden': (k.get('final_result') or {}).get('reason') or k.get('reason')})
        return basari

    def izin_bekle(exc: PermissionError, k: dict[str, Any]) -> int:
        if str(exc) == 'commit-permission-required':
            git_commit_bekle(store, run_id, Path(k['worktree']), k)
        d.update(durum='durdu', neden=str(exc), bekleyen_urun={'kosu': k.get('run_id'), 'worktree': k.get('worktree'),
                 'revision': (k.get('final_result') or {}).get('revision')})
        store.update(run_id, reason='proje-durdu: ' + str(exc))
        kaydet()
        return 78

    def checkpoint_wait(feature: str, child: dict[str, Any], success: bool) -> int:
        import uuid

        from kosu import plan_digest
        d.update(durum='durdu', neden='project-step-verification-required', checkpoint={
            'id': uuid.uuid4().hex, 'feature': feature, 'run_id': child['run_id'], 'success': success,
            'status': 'waiting', 'created': simdi(), 'final_digest': plan_digest(child.get('final_result') or {})})
        kaydet()
        store.update(run_id, reason='proje-durdu: project-step-verification-required')
        return 78

    def yeniden(son_kabul: dict[str, Any] | None = None) -> bool:
        try:
            raw = planla(yeniden_plan_istemi(istek, d, son_kabul, ekler), 'proje-yeniden') if son_kabul is not None else ''
            features = yeniden_plan_birlestir(d['ozellikler'], raw) if raw else d['ozellikler']
            features = proje_plani_kaydet(store, run_id, features, raw)
            view = store.plan_view(run_id) if hasattr(store, 'home') else {}
            d.setdefault('plan_degerlendirmeleri', []).append({'ts': simdi(), 'reason': 'final-acceptance-failed' if son_kabul else 'progress-or-stall',
                'decision': 'revised' if features != d['ozellikler'] else 'unchanged',
                'plan_revision': view.get('revision'), 'plan_digest': (view.get('history') or [{}])[-1].get('digest'),
                'source_sha': store.read(run_id).get('source_sha'),
                'evidence': {'completed': d['bitirilen'], 'stalled': [f['id'] for f in features if f['durum'] == 'takildi']}})
            d['ozellikler'] = features
            proje_tavani_yaz(store, run_id, d['ozellikler'])
            return True
        except (ValueError, RuntimeError, OSError) as exc:
            d['son_plan_hatasi'] = str(exc)[:300]
            d.update(durum='durdu', neden='project-plan-review-required')
            store.update(run_id, reason='proje-durdu: project-plan-review-required')
            return False

    if len(istek) > PLAN_ISTEK_TAVANI:
        d['durum'], d['neden'] = 'durdu', f'istek-cok-uzun: {len(istek)} karakter (tavan {PLAN_ISTEK_TAVANI})'
        kaydet()
        return 1
    d.update(durum='calisiyor', neden='', asama='')
    store.update(run_id, reason=None)
    kaydet()
    if d.pop('replan_pending', False):
        if not yeniden():
            kaydet()
            return 78
        kaydet()
    while True:
        if (kok / 'cancel.flag').exists():
            d['durum'], d['neden'] = 'durdu', 'iptal'
            break
        if simdi() - d['baslangic'] >= SURE_TAVANI_SN:
            d.update(durum='durdu', neden='sure-tavani')
            break
        if not d['ozellikler']:
            try:
                raw = planla(liste_istemi(istek, harita, ekler, ilk_plan_ozeti(kok)), 'proje-liste')
                features = proje_plani_kaydet(store, run_id, ozellik_listesi(raw), raw)
                d['ozellikler'] = features
            except (ValueError, RuntimeError, OSError) as exc:
                d['durum'], d['neden'], d['son_plan_hatasi'] = 'durdu', 'liste-uretilemedi', str(exc)[:300]
                break
            kaydet()
        proje_tavani_yaz(store, run_id, d['ozellikler'])
        dur, neden = dur_karari(d, simdi())
        if dur:
            d['durum'], d['neden'] = 'durdu', neden
            break
        if neden == 'son-kabul':
            store.update(run_id, project_active_feature=None)
            k = cocuk_calistir(son_kabul_istemi(istek, d, ekler), d['ara_dal'])
            if bekleme_nedeni(k):
                if supervised:
                    return checkpoint_wait('son-kabul', k, False)
                d['durum'], d['neden'] = 'durdu', bekleme_nedeni(k)
                kaydet()
                return 1
            try:
                accepted = cocuk_isle(k, 'son-kabul')
            except PermissionError as exc:
                return izin_bekle(exc, k)
            if accepted:
                for f in d['ozellikler']:
                    if f['durum'] == 'uygulandi':
                        f['durum'] = 'gecti'
                    elif f['durum'] == 'takildi':
                        f['durum'], f['son_kabulde'] = 'gecti', True
                d['durum'], d['neden'], d['son_cocuk'] = 'tamam', 'son-kabul-gecti', k.get('run_id')
                store.update(run_id, worktree=k.get('worktree'), final_result=k.get('final_result'),
                             final_acceptance_run_id=k['run_id'])
                break
            if supervised:
                return checkpoint_wait('son-kabul', k, False)
            d['son_kabul_turu'] += 1
            if not yeniden(k):
                break
            kaydet()
            continue
        retry = d.pop('retry_feature', None)
        f = next((item for item in d['ozellikler'] if item['id'] == retry), None) if retry else siradaki(d['ozellikler'])
        if f is None or f['durum'] != 'bekliyor':
            raise ValueError('project-step-retry-feature-invalid')
        if supervised and len(f.get('source_items', [])) > 1:
            raise ValueError('project-step-multiple-source-items')
        store.update(run_id, project_active_feature=f['id'])
        k = cocuk_calistir(ozellik_istemi(istek, d, f, istek_yolu), d['ara_dal'])
        if bekleme_nedeni(k):
            if supervised:
                return checkpoint_wait(f['id'], k, False)
            d['durum'], d['neden'] = 'durdu', bekleme_nedeni(k)
            kaydet()
            return 1
        try:
            basari = cocuk_isle(k, f['id'])
        except PermissionError as exc:
            return izin_bekle(exc, k)
        yeni = sonuc_isle(f, str(k.get('run_id')), basari)
        d['ozellikler'] = [yeni if g['id'] == f['id'] else g for g in d['ozellikler']]
        if basari:
            d['son_gecti'], d['ardisik_basarisiz'], d['bitirilen'] = simdi(), 0, d['bitirilen'] + 1
        else:
            d['ardisik_basarisiz'] += 1
        needs_replan = yeni['durum'] == 'takildi' or (basari and d['bitirilen'] % 3 == 0 and siradaki(d['ozellikler']) is not None)
        if supervised:
            d['replan_pending'] = needs_replan
            return checkpoint_wait(f['id'], k, basari)
        if needs_replan:
            if not yeniden():
                break
        kaydet()
    kaydet()
    return 0 if d['durum'] == 'tamam' else 1


# --- Git and project settings (G-P3): only `cadre/proje-*` is written, the canonical branch is not touched ---

ARA_DAL = re.compile(r'cadre/proje-[a-z0-9]{1,32}')
TURETILEN_DOSYALAR = ('repo-path', 'base-branch', 'PROJECT.md', 'POLICY.md', 'STATE.md', 'qa-komut', 'kurulum-komut', 'kosu-tavan-usd',
                      'kosu-tavan-zor-usd', 'bicim-komut')   # B272: the hard cap and the format command are inherited too


def _git(yer: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if args[:1] == ('status',):
        from sonuc import git_read_environment
        with git_read_environment(yer) as env:
            return subprocess.run(['git', '-C', str(yer), *args], capture_output=True, text=True, timeout=120, check=check, env=env)
    return subprocess.run(['git', '-C', str(yer), *args], capture_output=True, text=True, timeout=120, check=check,
                          env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})


def ara_dal_hazirla(repo: str | Path, ara_dal: str, sha: str, kaynak: dict[str, Any] | None = None) -> None:
    if not ARA_DAL.fullmatch(ara_dal):
        raise ValueError('ara dal yalniz cadre/proje-<kimlik> olabilir')
    if _git(repo, 'show-ref', '--verify', '--quiet', f'refs/heads/{ara_dal}', check=False).returncode == 0:
        return
    _git(repo, 'branch', ara_dal, kabul_goruntusu(repo, sha, kaynak or {}))


def _olumsuz_anilmis(istek: str, bas: int, son: int) -> bool:
    once = re.split(r'[.!?;,:]\s|\n', istek[max(0, bas - 120):bas])[-1]
    sonra = re.split(r'[.!?;,:]\s|\n', istek[son:son + 120])[0]
    return bool(OLUMSUZ.search(once + ' ' + sonra))


def istek_ekleri(repo: str | Path, ref: str, istek: str) -> str:
    dosyalar = [y for y in _git(repo, 'ls-tree', '-r', '--name-only', ref, check=False).stdout.splitlines()
                if y and Path(y).suffix.lower() in BELGE_UZANTILARI and not HASSAS_YOL.search(y)]

    def anilir(kalip: str) -> bool:
        return any(not _olumsuz_anilmis(istek, m.start(), m.end()) for m in re.finditer(kalip, istek, re.I))
    adlar: dict[str, list[str]] = {}
    for yol in dosyalar:
        adlar.setdefault(Path(yol).name.casefold(), []).append(yol)
    secilen = [y for y in dosyalar if '/' in y and anilir(re.escape(y))]
    for ad, yollar in adlar.items():
        if anilir(rf'(?<![\w/.-]){re.escape(ad)}(?![\w-])'):
            kokteki = [y for y in yollar if '/' not in y]
            secilen += [y for y in (kokteki or yollar) if y not in secilen]
    parcalar = []
    for yol in secilen:
        r = subprocess.run(['git', '-C', str(repo), 'show', f'{ref}:{yol}'], capture_output=True, timeout=60)
        if r.returncode == 0 and b'\0' not in r.stdout[:8192]:
            metin = r.stdout.decode('utf-8', 'replace')
            if len(metin) > EK_DOSYA_TAVANI:
                metin = metin[:EK_DOSYA_TAVANI] + f"\n[... dosyanin tamami {len(metin)} karakter: {yol}; kalan maddeler cocuk kosularda bu dosyadan okunur]"
            parcalar.append(f'--- {yol} ---\n' + metin)
    return '\n'.join(parcalar)


def kabul_goruntusu(repo: str | Path, sha: str, kaynak: dict[str, Any]) -> str:
    yama, kopya = kaynak.get('patch'), Path(str(kaynak.get('untracked') or '-'))
    if not kaynak.get('carried') or not (yama or kopya.is_dir()):
        return sha
    raise PermissionError('source-commit-permission-required')


def git_goruntu_agaci(repo: Path, sha: str, kaynak: dict[str, Any] | None = None) -> str:
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cadre-git-proof-') as temporary:
        root = Path(temporary)
        objects = root / 'objects'
        objects.mkdir()
        alternate = Path(_git(repo, 'rev-parse', '--git-path', 'objects').stdout.strip())
        alternate = alternate if alternate.is_absolute() else repo / alternate
        env = {**os.environ, 'GIT_INDEX_FILE': str(root / 'index'), 'GIT_OBJECT_DIRECTORY': str(objects),
               'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(alternate.resolve()), 'GIT_OPTIONAL_LOCKS': '0'}
        def git(*args: str) -> str:
            return subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True,
                                  text=True, check=True, timeout=120).stdout.strip()
        git('read-tree', sha)
        if kaynak is not None:
            if kaynak.get('patch'):
                git('apply', '--cached', '--binary', '--whitespace=nowarn', '--allow-empty', str(kaynak['patch']))
            copy_root = Path(str(kaynak.get('untracked') or '-'))
            if copy_root.is_dir():
                for path in sorted(copy_root.rglob('*')):
                    if path.is_file() and not path.is_symlink():
                        blob = git('hash-object', '-w', '--', str(path))
                        mode = '100755' if path.stat().st_mode & 0o111 else '100644'
                        git('update-index', '--add', '--cacheinfo', f'{mode},{blob},{path.relative_to(copy_root).as_posix()}')
        else:
            from qe_core import URUN_HARIC
            git('add', '-A', '--', '.')
            excluded = [p.replace(':(exclude,glob)', ':(glob)').replace(':(exclude)', '') for p in URUN_HARIC]
            git('rm', '--cached', '-r', '-q', '--ignore-unmatch', '--', *excluded)
        return git('write-tree')


def git_commit_bekle(store: Any, run_id: str, repo: Path, child: dict[str, Any] | None = None) -> None:
    from kosu import FileLock, atomic_json, plan_digest
    from sonuc import approved, implemented, revision
    if child and not (approved(child) or implemented(child)):
        raise ValueError('project-commit-unverified-product')
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        head = child['final_result']['revision']['head'] if child else r['source_sha']
        pending = {'kind': 'product' if child else 'source', 'path': str(repo.resolve()), 'head': head,
                   'tree': git_goruntu_agaci(repo, head, None if child else r.get('source') or {}),
                   'child': child['run_id'] if child else '', 'result_digest': plan_digest(child['final_result']) if child else '',
                   'plan_revision': store.plan_view(run_id)['revision'], 'source_sha': r['source_sha']}
        if child and revision(repo) != child['final_result']['revision']:
            raise ValueError('project-commit-evidence-mismatch')
        r['project_git_wait'] = pending
        atomic_json(store.path(run_id) / 'run.json', r)


def git_commit_devral(store: Any, run_id: str, sha: str) -> dict[str, Any]:
    from contextlib import ExitStack
    from kosu import FileLock, atomic_json, plan_digest, plan_hold, project_time_expired, TERMINAL
    from sonuc import revision, approved, implemented
    if not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', sha):
        raise ValueError('invalid-project-commit')
    with ExitStack() as locks:
        owner = FileLock(store.path(run_id) / '.owner.lock')
        if not owner.acquire(blocking=False):
            raise ValueError('project-commit-owner-active')
        locks.callback(owner.close)
        locks.enter_context(FileLock(store.path(run_id) / '.record.lock'))
        r = store.read(run_id)
        pending = r.get('project_git_wait') or {}
        if (not pending or r['state'] in TERMINAL or plan_hold(r) or project_time_expired(r)
                or (store.path(run_id) / 'cancel.flag').exists()
                or pending['plan_revision'] != store.plan_view(run_id)['revision']
                or pending['source_sha'] != r['source_sha']):
            raise ValueError('stale-project-commit')
        if pending.get('adopted_sha'):
            if pending['adopted_sha'] != sha:
                raise ValueError('stale-project-commit')
            return pending
        repo = Path(pending['path'])
        if _git(repo, 'rev-list', '--parents', '-n', '1', sha).stdout.split() != [sha, pending['head']]:
            raise ValueError('project-commit-parent-mismatch')
        if pending['kind'] == 'source':
            if (_git(repo, 'rev-parse', f'{sha}^{{tree}}').stdout.strip() != pending['tree']
                    or git_goruntu_agaci(repo, r['source_sha'], r.get('source') or {}) != pending['tree']):
                raise ValueError('project-commit-content-mismatch')
            r['project_source_commit'] = {'sha': sha, 'tree': pending['tree'], 'source_sha': r['source_sha']}
        else:
            child_id = pending['child']
            child_owner = FileLock(store.path(child_id) / '.owner.lock')
            if not child_owner.acquire(blocking=False):
                raise ValueError('project-commit-owner-active')
            locks.callback(child_owner.close)
            locks.enter_context(FileLock(store.path(child_id) / '.record.lock'))
            child = store.read(child_id)
            from qe_core import URUN_HARIC
            if (child.get('proje_ust') != run_id or child['state'] not in ('completed', 'implemented')
                    or Path(child['worktree']).resolve() != repo
                    or not _git(repo, 'branch', '--show-current').stdout.strip().startswith('cadre/')
                    or _git(repo, 'rev-parse', 'HEAD').stdout.strip() != sha
                    or _git(repo, 'status', '--porcelain', '--', '.', *URUN_HARIC).stdout.strip()
                    or git_goruntu_agaci(repo, sha) != pending['tree']):
                raise ValueError('project-commit-content-mismatch')
            result = child['final_result']
            proof = {'sha': sha, 'tree': pending['tree'], 'previous_result': pending['result_digest']}
            if result.get('commit_adoption') != proof:
                if plan_digest(result) != pending['result_digest']:
                    raise ValueError('project-commit-evidence-mismatch')
                current = revision(repo)
                result = {**result, 'revision': current, 'qa': {**result['qa'], 'revision': current},
                          'review': {**result.get('review', {}), 'revision': current}, 'commit_adoption': proof}
                if result.get('delivery'):
                    result['delivery'] = {**result['delivery'], 'revision': current, 'git_permission': 'operator-committed'}
                candidate = {**child, 'final_result': result}
                if not (approved(candidate) or implemented(candidate)):
                    raise ValueError('project-commit-evidence-mismatch')
                child['final_result'] = result
                atomic_json(store.path(child_id) / 'run.json', child)
            elif not (approved(child) or implemented(child)):
                raise ValueError('project-commit-evidence-mismatch')
        pending['adopted_sha'] = sha
        from kosu import mudahale_ekle
        mudahale_ekle(r, 'git-adopt', {'sha': sha, 'kind': pending['kind']})
        r.update(reason=None, project_git_wait=pending)
        atomic_json(store.path(run_id) / 'run.json', r)
        return pending


def git_aktarim_onayi(store: Any, run_id: str, token: str, confirmation: str = '') -> dict[str, Any]:
    from kosu import FileLock, atomic_json, plan_digest, plan_hold
    import uuid
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        action = r.get('project_git_action') or {}
        if (not action or action['token'] != token or plan_digest(action['identity']) != token or plan_hold(r)
                or action['identity']['plan_revision'] != store.plan_view(run_id)['revision']
                or r['state'] in ('completed', 'failed', 'cancelled', 'implemented')
                or (store.path(run_id) / 'cancel.flag').exists()):
            raise ValueError('stale-git-action')
        if action['status'] == 'pending' and not confirmation:
            action.update(status='first-approved', confirmation=uuid.uuid4().hex)
        elif action['status'] == 'first-approved' and confirmation and confirmation == action.get('confirmation'):
            action.update(status='approved')
        else:
            raise ValueError('git-second-approval-required')
        from kosu import mudahale_ekle
        mudahale_ekle(r, 'git-transfer-approve', {'stage': 2 if confirmation else 1})
        atomic_json(store.path(run_id) / 'run.json', r)
        return action


def git_aktar(store: Any, run_id: str, repo: str | Path, ref: str, yeni: str, eski: str) -> bool:
    from kosu import FileLock, atomic_json, plan_digest, plan_hold
    command = ['git', '-C', str(Path(repo).resolve()), 'update-ref', ref, yeni, eski]
    if (not ref.startswith('refs/heads/') or not ARA_DAL.fullmatch(ref.removeprefix('refs/heads/'))
            or any(not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', sha) for sha in (yeni, eski))):
        raise ValueError('invalid-project-git-action')
    if store is None:
        raise PermissionError('git-transfer-permission-required')
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        if (r['state'] in ('completed', 'failed', 'cancelled', 'implemented') or plan_hold(r)
                or (store.path(run_id) / 'cancel.flag').exists()):
            raise PermissionError('git-transfer-stopped')
        action = r.get('project_git_action') or {}
        identity = {'command': command, 'source_sha': r['source_sha'], 'plan_revision': store.plan_view(run_id)['revision']}
        token = plan_digest(identity)
        if action.get('identity') != identity or action.get('status') in ('consumed', 'failed'):
            action = {'command': command, 'identity': identity, 'token': token, 'status': 'pending'}
            r['project_git_action'] = action
            atomic_json(store.path(run_id) / 'run.json', r)
        if action.get('status') != 'approved' or action.get('token') != token:
            raise PermissionError('git-transfer-permission-required')
        action.update(status='consumed')
        atomic_json(store.path(run_id) / 'run.json', r)
        return _git(repo, 'update-ref', ref, yeni, eski, check=False).returncode == 0


def birlestir_ff(repo: str | Path, ara_dal: str, k: dict[str, Any], *, store: Any = None, run_id: str = '') -> bool:
    """Fast-forwards the clean product of a completed child onto the intermediate branch (update-ref checked against the old tip); False in every other case."""
    wt = Path(str(k.get('worktree') or ''))
    if k.get('state') not in ('completed', 'implemented') or not ARA_DAL.fullmatch(ara_dal) or not wt.is_dir():
        return False
    if not _git(wt, 'branch', '--show-current', check=False).stdout.strip().startswith('cadre/'):
        return False
    if k.get('state') == 'implemented':
        from sonuc import implemented
        if not implemented(k):
            return False
    from qe_core import URUN_HARIC   # B234: the dirt measure is the same as the engine's; engine leftovers like `.gorevler.json` are not product
    if _git(wt, 'status', '--porcelain', '--', '.', *URUN_HARIC, check=False).stdout.strip():
        raise PermissionError('commit-permission-required')
    yeni = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    eski = _git(repo, 'rev-parse', f'refs/heads/{ara_dal}').stdout.strip()
    if yeni == eski:
        return True
    if _git(repo, 'merge-base', '--is-ancestor', eski, yeni, check=False).returncode != 0:
        return False
    return git_aktar(store, run_id, repo, f'refs/heads/{ara_dal}', yeni, eski)


def turet_proje(home: str | Path, proje_adi: str, kimlik: str) -> str:
    """Project of the child runs: same repo and settings, a separate queue (a child must not wait in line while the main project run is going)."""
    from kosu import atomic_write, identifier
    ad = identifier(f'{proje_adi}--p{kimlik}')
    hedef = Path(home) / 'projects' / ad
    hedef.mkdir(parents=True, exist_ok=True)
    for dosya in TURETILEN_DOSYALAR:
        kaynak = Path(home) / 'projects' / proje_adi / dosya
        if kaynak.is_file():
            atomic_write(hedef / dosya, kaynak.read_bytes())
    return ad


# --- Real bindings: planner (plan chain), child run (RunStore + execute_run), entry command ---

def _planlayici_cagir(store: Any, ana_rid: str, model: str, istem: str, gorev_id: str) -> str:
    from kosu import capture_source_plan
    from sinirlar import son_sure
    if store.read(ana_rid).get('supervision') == STEP_POLICY:
        first, _, rest = istem.partition('\n')
        istem = first + '\n' + STEP_INSTRUCTIONS + rest
    return capture_source_plan(store, ana_rid, os.environ.get('CADRE_REAL_OPENCODE') or '/opt/homebrew/bin/opencode',
                               model, istem, gorev_id, son_sure('plan'), 4)


def planla_gercek(store: Any, ana_rid: str) -> Callable[[str, str], str]:
    """The first three links of the plan chain in order; returns the first answer that carries `ozellikler`."""
    def plan_gecmisi() -> list[dict[str, Any]]:
        # B252: dependencies on ids kept in a re-plan (passed/stuck) are valid; link validation must know this.
        try:
            d = json.loads((Path(store.path(ana_rid)) / 'proje.json').read_text())
            features = d['ozellikler']
            if not isinstance(features, list) or any(not isinstance(f, dict) or f.get('durum') not in
                    ('gecti', 'uygulandi', 'takildi', 'bekliyor') for f in features):
                raise ValueError('yeniden-plan-gecmisi-gecersiz')
            return features
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            raise ValueError('yeniden-plan-gecmisi-okunamadi') from None

    def planla(istem: str, gorev_id: str) -> str:
        from yonlendirici import zincir
        z, _ = zincir('plan', 'zor', max(30000, len(istem) // 3))
        hatalar = []
        previous = plan_gecmisi() if gorev_id == 'proje-yeniden' else None
        for model in z[:3]:
            istem_ = istem
            for deneme in range(2):   # B252b: a validation error goes back to the same link ONCE with the error text
                try:
                    ham = _planlayici_cagir(store, ana_rid, model, istem_, gorev_id)
                    # B250: a list that passes the rough check but fails full validation (truncated last item) is left to the next link
                    if previous is not None:
                        yeniden_plan_birlestir(previous, ham)
                    else:
                        _dogrula(_ham_liste(ham))
                    return ham
                except ValueError as exc:
                    if str(exc) in ('plan-review-source-modified', 'plan-review-session-unverified',
                                    'request-or-plan-change-gate', 'request-plan-context-limit', 'plan-review-owner-active'):
                        raise
                    hatalar.append(f'{model}: {str(exc)[:120]}')
                    if deneme:
                        break
                    istem_ = (istem + f"\n\nONCEKI CEVABIN GECERSIZDI ({exc}). Yalniz bu listedeki ya da korunan kimliklere "
                              "bagimlilik ver, her maddede id/baslik/aciklama/kabul/qa_komut dolu olsun; SADECE tek JSON blogu.")
                except PermissionError:
                    raise
                except Exception as exc:   # one link's failure does not prevent trying the others
                    hatalar.append(f'{model}: {str(exc)[:120]}')
                    break
        raise RuntimeError('planlayici gecerli liste vermedi: ' + '; '.join(hatalar))
    return planla


KAPASITE_BEKLEME_TAVANI = 3   # B241: at most this many judge-capacity waits for one child


def _tuketildi(store: Any, ana_rid: str, cid: str) -> bool:
    """Has the parent recorded this child's result in `proje.json` (G-P4 single writer; re-entry is idempotent)."""
    try:
        d = json.loads((store.path(ana_rid) / 'proje.json').read_text())
        return any(c.get('kosu') == cid for c in d.get('cocuklar') or [])
    except (OSError, ValueError, AttributeError):
        return False


def cocuk_calistir_gercek(store: Any, ana_rid: str, cocuk_proje: str, istem: str, base_ref: str,
                          calistir: Callable[[Any, str], int] | None = None) -> dict[str, Any]:
    """A child run opens from the tip of the intermediate branch and runs in the same process; an interrupted child (same prompt) continues without opening a new run."""
    import hashlib
    from kosu import TERMINAL, atomic_json, execute_run
    calistir = calistir or execute_run
    aktif_yol = store.path(ana_rid) / 'proje-aktif.json'
    imza = hashlib.sha256(istem.encode()).hexdigest()
    aktif = json.loads(aktif_yol.read_text()) if aktif_yol.exists() else {}
    cid = aktif.get('cocuk') if aktif.get('imza') == imza else None
    if cid:
        parent = store.read(ana_rid)
        store.update(cid, proje_ust=ana_rid, proje_ozellik=parent.get('project_active_feature'))
        store.update(ana_rid, project_active_run=cid, project_run_ids=sorted(set(parent.get('project_run_ids') or [ana_rid]) | {cid}))
    if cid and store.read(cid).get('state') in TERMINAL and not _tuketildi(store, ana_rid, cid):
        return store.read(cid)   # B292: the parent was interrupted before recording this result; the same job does not run twice
    if not cid or store.read(cid).get('state') in TERMINAL:
        cid = store.accept(cocuk_proje, istem, base_ref=base_ref, parent_run_id=ana_rid)['run_id']
        parent = store.read(ana_rid)
        feature = parent.get('project_active_feature')
        store.update(cid, proje_ust=ana_rid, proje_ozellik=feature)
        store.update(ana_rid, project_active_run=cid, project_run_ids=sorted(set(parent.get('project_run_ids') or [ana_rid]) | {cid}))
        atomic_json(aktif_yol, {'imza': imza, 'cocuk': cid})
    parent = store.read(ana_rid)
    supervised = parent.get('supervision') == STEP_POLICY
    fid = parent.get('project_active_feature')
    try:
        state = json.loads((store.path(ana_rid) / 'proje.json').read_text())
    except (OSError, ValueError):
        state = {}
    if fid:
        feature = next(f for f in state['ozellikler'] if f['id'] == fid)
        store.update(cid, feature_acceptance={k: feature.get(k) for k in ('id', 'kabul', 'qa_komut', 'riskli',
                                                                        'source_tasks', 'source_items', 'source_acceptance')})
    else:
        ek = [c for c in (_kabul_komutu(f) for f in state.get('ozellikler') or [] if f.get('durum') == 'takildi') if c]
        if ek:
            store.update(cid, ek_kabul_komutlari=ek)
    try:
        bitis = float(state['baslangic']) + SURE_TAVANI_SN
    except (KeyError, TypeError, ValueError):
        bitis = float('inf')
    calistir(store, cid)
    son = store.read(cid)
    tur = 0
    # B268: CADRE_BEKLEME_ESIK_SN=0 disables waiting (e2e fixtures hung on the clock);
    # bekleme_esik=0 -> no sleep but the B241/B273 retry logic runs, at most 1 round.
    bekleme_esik = int(os.environ.get("CADRE_BEKLEME_ESIK_SN", "30"))
    retry_tavani = 1 if bekleme_esik == 0 else KAPASITE_BEKLEME_TAVANI
    # B241: a child waiting for judge capacity (retryable + a future retry_after) is NOT failed; when the share renews
    # the same run continues (lanes that produced are skipped, acceptance again). Otherwise a passed feature was redone from scratch.
    from kosu import kosu_tavani_tukendi
    while not supervised and son.get('state') == 'retryable' and float(son.get('retry_after') or 0) > time.time() and time.time() < bitis and not _alarm_bekliyor(son) and not kosu_tavani_tukendi(son, store.home) and ((bekleme_esik != 0 and str(son.get('reason') or '').startswith('butce-bekleme')) or tur < retry_tavani):   # if waiting is disabled, the budget wait is also limited to the round
        hedef = min(float(son['retry_after']), bitis)
        while time.time() < hedef and not (store.path(ana_rid) / 'cancel.flag').exists():
            if bekleme_esik == 0:
                break   # no wait; try immediately
            time.sleep(min(60.0, max(0.1, hedef - time.time())))
        if (store.path(ana_rid) / 'cancel.flag').exists() or time.time() >= bitis:
            break
        tur += 1
        calistir(store, cid)
        son = store.read(cid)
    if _alarm_bekliyor(son):
        store.update(ana_rid, harcama_alarmi=son['harcama_alarmi'])
        return son
    if bekleme_nedeni(son) and time.time() < bitis:
        if son.get('retry_after'):
            store.update(ana_rid, retry_after=son['retry_after'])
        return son
    # B273: the coordinator leaves 'retryable' with rc!=0 (no retry_after); a normal run is retried overnight via P4,
    # but a child counted as 'feature failed' after a single attempt. ONE retry allowance for the same run (if not cancelled).
    if not supervised and son.get('state') == 'retryable' and not son.get('retry_after') and not (store.path(ana_rid) / 'cancel.flag').exists():
        store.update(cid, state='accepted', reason='', retry_count=int(son.get('retry_count') or 0) + 1)
        calistir(store, cid)
        son = store.read(cid)
    if son.get('state') not in TERMINAL and time.time() >= bitis:
        son = store.update(cid, state='failed', reason=f"proje-sure-tavani: {son.get('reason') or ''}".strip())
    if son.get('state') not in TERMINAL:   # must not hold the next child's queue; the decision is in the project loop
        son = store.update(cid, state='failed', reason=f"proje-ozellik-basarisiz: {son.get('state')} {son.get('reason') or ''}".strip())
    return son


def _alarm_bekliyor(k: dict[str, Any]) -> bool:
    alarm = k.get('harcama_alarmi') or {}
    return bool(alarm) and float(alarm.get('ts') or 0) > float(k.get('harcama_onayi_ts') or 0)


def bekleme_nedeni(k: dict[str, Any]) -> str:
    if k.get('state') == 'retryable' and str(k.get('tool_boundary_wait') or '').startswith('project-tool-'):
        return k['tool_boundary_wait']
    if _alarm_bekliyor(k):
        return 'harcama-alarmi'
    if k.get('state') == 'retryable' and float(k.get('retry_after') or 0) > time.time():
        return 'kapasite-bekleme'
    return ''


HARITA_SATIR = 80


def _harita(repo: str | Path) -> str:
    """N-REP03/N-PL01: in a large repo, map truncation is visible in the prompt; the map is not evidence of understanding."""
    satirlar = _git(repo, 'ls-files', check=False).stdout.splitlines()
    baslik = f'({len(satirlar)} dosya' + (f'; ilk {HARITA_SATIR} gösterildi, KIRPILDI' if len(satirlar) > HARITA_SATIR else '') + ')'
    return '\n'.join([baslik, *satirlar[:HARITA_SATIR]])


def uygun(kayit: dict[str, Any]) -> bool:
    """G-P1/N-RUN01: a supervised code or ultra (planner ≥66) job is split sequentially; a project child is not split again."""
    if kayit.get('supervision') == STEP_POLICY and kayit.get('job_type') == 'code' and not kayit.get('proje_ust'):
        return True
    k = kayit.get('kapsam') or {}
    try:
        planlayici = int(k.get('planlayici'))
    except (TypeError, ValueError):
        return False
    return k.get('seviye') == 'ultra' and planlayici >= 66 and not kayit.get('proje_ust')


def baslat(run_id: str, proje_adi: str, *, planla: Callable[[str, str], str] | None = None,
           calistir: Callable[[Any, str], int] | None = None) -> int:
    from kosu import RunStore, atomic_json
    store = RunStore()
    kayit = store.read(run_id)
    if supervision_waiting(store, kayit):
        return 78
    blocked = giris_engeli(kayit)
    if blocked is not None:
        if blocked == 75:
            store.update(run_id, reason='proje-durdu: harcama-alarmi')
        return blocked
    repo = Path((store.home / 'projects' / proje_adi / 'repo-path').read_text().strip())
    yol = store.path(run_id) / 'proje.json'
    istek = Path(kayit['prompt']).read_text(errors='replace')
    if not yol.exists():
        atomic_json(yol, _yeni_durum(run_id, istek, time.time()))   # identity and intermediate branch are pinned at the first moment
    d0 = json.loads(yol.read_text())
    proje_butcesi_sabitle(store, run_id, d0['baslangic'])
    from kosu import project_time_expired
    if project_time_expired(store.read(run_id)):
        store.update(run_id, reason='proje-durdu: sure-tavani')
        return 78
    try:
        accepted = (kayit.get('project_source_commit') or {}).get('sha') or kayit.get('source_sha') or 'HEAD'
        ara_dal_hazirla(repo, d0['ara_dal'], accepted, {} if kayit.get('project_source_commit') else kayit.get('source') or {})
    except PermissionError as exc:
        git_commit_bekle(store, run_id, repo)
        d0.update(durum='durdu', neden=str(exc))
        atomic_json(yol, d0)
        store.update(run_id, reason='proje-durdu: ' + str(exc))
        return 78
    from kosu import FileLock, planning_environment, taban_qa_kaydet
    import qe_core as core
    with FileLock(store.path(run_id) / '.project-baseline.lock'), planning_environment(store):
        os.environ.update(store.environment(run_id))
        baseline_source = store.path(run_id) / 'baseline-source'
        if not baseline_source.exists():
            _git(repo, 'worktree', 'add', '--detach', str(baseline_source), accepted)
        baseline = core.taban_hazirlik(proje_adi, baseline_source, store.path(run_id) / 'baseline.log',
            store.path(run_id) / 'taban-qa.json', store.home / 'projects' / proje_adi)
        taban_qa_kaydet(store, run_id, baseline)
        if baseline.get('status') != 'PASS':
            store.update(run_id, reason=baseline.get('reason') or 'project-baseline-unavailable')
            return 1
    ekler = istek_ekleri(repo, d0['ara_dal'], istek)
    cocuk_proje = turet_proje(store.home, proje_adi, d0['kimlik'])
    store.update(run_id, proje={'kimlik': d0['kimlik'], 'ara_dal': d0['ara_dal'], 'cocuk_proje': cocuk_proje})
    rc = yurut(store, run_id, planla=planla or planla_gercek(store, run_id),
               cocuk_calistir=lambda istem, dal: cocuk_calistir_gercek(store, run_id, cocuk_proje, istem, dal, calistir),
               birlestir=lambda dal, k: birlestir_ff(repo, dal, k, store=store, run_id=run_id), harita=_harita(repo), ekler=ekler)
    d = json.loads(yol.read_text())
    if rc != 0 and d.get('neden') != 'iptal':
        store.update(run_id, reason=f"proje-durdu: {d.get('neden')}")
    return rc


def durum_ozeti(store: Any, proje_adi: str) -> str:
    """One line for `cadre durum`: progress of the project's newest project run; empty without a project run."""
    kosular = [r for r in store.list(proje_adi) if r.get('proje')]
    if not kosular:
        return ''
    son = max(kosular, key=lambda r: r.get('created') or 0)
    yol = store.path(son['run_id']) / 'proje.json'
    if not yol.exists():
        return f"{son['proje'].get('ara_dal', '?')} · hazirlaniyor"
    d = durum_gorunumu(son, json.loads(yol.read_text()))
    liste = d.get('ozellikler') or []
    gecen = sum(1 for f in liste if f.get('durum') == 'gecti')
    d['maliyet'] = proje_maliyeti(store.home, [son['run_id']])
    return (f"{d.get('ara_dal', '?')} · {gecen}/{len(liste)} gecti · {d.get('durum', '?')}"
            + (f" ({d['neden']})" if d.get('neden') else '') + (f" · {d['asama']}" if d.get('asama') else '') + ' · ' + _harcama_satiri(d)
            + f" · rapor {store.path(son['run_id']) / 'proje-raporu.md'}")


if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['uygun'] and len(sys.argv) >= 3:
        from kosu import RunStore
        raise SystemExit(0 if uygun(RunStore().read(sys.argv[2])) else 1)
    if sys.argv[1:2] == ['ozet'] and len(sys.argv) >= 3:
        from kosu import RunStore
        print(durum_ozeti(RunStore(), sys.argv[2]))
        raise SystemExit(0)
    if sys.argv[1:2] == ['baslat'] and len(sys.argv) >= 4:
        raise SystemExit(baslat(sys.argv[2], sys.argv[3]))
    print(__doc__)
    raise SystemExit(2)
