"""Versioned difficulty/preference policy that orders a role's allowed model pool (D16).
Measurements are reported; the model order never changes by itself. A suitable cheap route of the same model comes first."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import hashlib
import math
import re
from pathlib import Path
import time
from typing import Any

from katalog import kanonik_model, model as katalog_model, fiyat
from kosu import FileLock, atomic_json

OLCUM_OMRU = 7 * 86400
POLITIKA_SURUMU = 'difficulty-preference-role-v1'
ZORLUKLAR = ('easy', 'medium', 'hard', 'very_hard')


def gorev_secimi(prompt: str, values: dict[str, Any]) -> dict[str, Any]:
    from seviye import kural_tabani
    score = kural_tabani(prompt)
    estimate = ZORLUKLAR[0 if score <= 15 else 1 if score <= 40 else 2 if score <= 65 else 3]
    preference = values['preference']
    override = values['difficulty_override']
    if preference not in ('economic', 'balanced', 'strong') or override not in (None, *ZORLUKLAR):
        raise ValueError('invalid selection input')
    return {'policy_version': POLITIKA_SURUMU, 'preference': preference,
            'difficulty_estimate': estimate, 'estimate_score': score, 'estimate_source': 'request-rules',
            'estimate_reason': f'kural_tabani={score}', 'difficulty_override': override,
            'difficulty_effective': override or estimate,
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}


def secim_dogrula(value: Any) -> dict[str, Any]:
    required = {'policy_version', 'preference', 'difficulty_estimate', 'estimate_score', 'estimate_source',
                'estimate_reason', 'difficulty_override', 'difficulty_effective', 'prompt_sha256'}
    if (not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {'parent_run_id'}
            or value.get('policy_version') != POLITIKA_SURUMU
            or value.get('preference') not in ('economic', 'balanced', 'strong')
            or value.get('difficulty_estimate') not in ZORLUKLAR
            or value.get('difficulty_override') not in (None, *ZORLUKLAR)
            or value.get('difficulty_effective') != (value.get('difficulty_override') or value.get('difficulty_estimate'))
            or type(value.get('estimate_score')) is not int or not 0 <= value['estimate_score'] <= 100
            or value.get('estimate_source') != 'request-rules'
            or value.get('estimate_reason') != f"kural_tabani={value.get('estimate_score')}"
            or not isinstance(value.get('prompt_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['prompt_sha256'])):
        raise ValueError('invalid selection policy')
    score = value['estimate_score']
    if value['difficulty_estimate'] != ZORLUKLAR[0 if score <= 15 else 1 if score <= 40 else 2 if score <= 65 else 3]:
        raise ValueError('invalid difficulty estimate')
    if 'parent_run_id' in value:
        from kosu import identifier
        identifier(value['parent_run_id'])
    return dict(value)


def kosu_secimi(home: Path, run_id: str) -> dict[str, Any] | None:
    from kosu import RunStore
    record = RunStore(home).read(run_id)
    if 'model_selection' not in record:
        if record.get('request'):
            raise ValueError('selection policy missing')
        return None
    value = secim_dogrula(record['model_selection'])
    request = record.get('request')
    if request and (any(value[key] != request[key] for key in ('preference', 'difficulty_override'))
                    or value['prompt_sha256'] != hashlib.sha256(request['prompt'].encode()).hexdigest()):
        raise ValueError('selection input mismatch')
    scope = record.get('kapsam') or {}
    score = scope.get('puan')
    if score is not None:
        if type(score) is not int or not 0 <= score <= 100:
            raise ValueError('invalid plan difficulty')
        value['plan_score'] = score
        if value['difficulty_override'] is None:
            score = max(score, value['estimate_score'])
            value['difficulty_effective'] = ZORLUKLAR[0 if score <= 15 else 1 if score <= 40 else 2 if score <= 65 else 3]
    return value


def politika_sirasi(adaylar: list[str], rol: str, selection: dict[str, Any]) -> tuple[list[str], str]:
    preference, difficulty = selection['preference'], selection['difficulty_effective']
    economical = (preference == 'economic' or preference == 'balanced' and difficulty == 'easy') and difficulty in ('easy', 'medium')
    economical = economical and rol not in ('kurtarici', 'birlestirme')
    ordered = sorted(adaylar, key=lambda entry: 0 if any(kanonik_model(route) == 'glm53' for route in entry.split('|')) else 1) if economical else list(adaylar)
    reason = (f"{POLITIKA_SURUMU}: tercih={preference}, zorluk={difficulty}, rol={rol}, "
              f"kural={'approved-glm-first' if economical else 'approved-role-order'}")
    return ordered, reason


EFOR_SEVIYELERI = ('none', 'minimal', 'low', 'medium')


def efor_secimi(selection: dict[str, Any] | None, rol: str, hata_sayisi: int, gorev: str = '') -> dict[str, str] | None:
    """N-TS03 (CR-50): explicit, versioned and reversible economy effort; off by default. Plan/review/judge/repair,
    hard work and a retry after an error run with the default effort."""
    from sinirlar import EFOR
    seviye = EFOR.get('kolay_serit')
    if seviye not in EFOR_SEVIYELERI or not isinstance(EFOR.get('surum'), str) or not selection:
        return None
    if rol != 'impl' or hata_sayisi or str(gorev).startswith(('duzeltme', 'plan', 'kalite', 'qa', 'birlestirme')):
        return None
    if selection.get('difficulty_effective') != 'easy' or selection.get('preference') not in ('economic', 'balanced'):
        return None
    return {'politika': EFOR['surum'], 'seviye': seviye}


def cagri_tavani(hedef: str, token: int, output: int, home: Path) -> int:
    provider, model = hedef.split('/', 1)
    data = katalog_model(provider, model)
    if not isinstance(data, dict):
        raise ValueError('model katalogda yok')
    limit = data.get('limit') or {}
    if type(limit.get('output')) is not int or limit['output'] <= 0:
        raise ValueError('cikti kapasitesi bilinmiyor veya gecersiz')
    output = min(output, limit['output'])
    if type(limit.get('context')) is not int or limit['context'] < token + output:
        raise ValueError('baglam kapasitesi bilinmiyor veya yetersiz')
    costs = data.get('cost') or {}
    if provider == 'openrouter':
        from butce_kapisi import or_pin_fiyati
        if not or_pin_fiyati(home, model, output):
            raise ValueError('OR-PIN fiyati yok')
        costs = json.loads((home / 'zincir/openrouter-fiyat.json').read_text())['modeller'][model]
    try:
        rows = [costs, *(costs.get('tiers') or [])]
        inputs = [Decimal(str(row['input'])) for row in rows]
        outputs = [Decimal(str(row['output'])) for row in rows]
        writes = [Decimal(str(row.get('cache_write', 0))) for row in rows]
        if not all(n.is_finite() and n >= 0 for n in (*inputs, *outputs, *writes)):
            raise ValueError('gecersiz fiyat')
        return int(((max(inputs) + max(writes)) * token + max(outputs) * output).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, KeyError, TypeError, OverflowError) as exc:
        raise ValueError('fiyat bilinmiyor') from exc


@dataclass(frozen=True)
class ModelOlcumu:
    kimlik: str
    dogru: int
    toplam: int
    saniye: float

    @property
    def puan(self) -> float:
        return (self.dogru + 1) / (self.toplam + 2)


def _kayitlar(home: Path) -> list[dict]:
    try:
        data = json.loads((home / 'zincir/model-olcumleri.json').read_text())
        return data['olcumler'] if data.get('surum') == 1 and isinstance(data.get('olcumler'), list) else []
    except (OSError, ValueError, AttributeError):
        return []


def olcum_yaz(home: Path, hedef: str, rol: str, dogru: int, toplam: int, saniye: float,
              simdi: float | None = None) -> None:
    if (type(dogru) is not int or type(toplam) is not int or not 0 <= dogru <= toplam or toplam <= 0
            or not math.isfinite(saniye) or saniye < 0):
        raise ValueError('gecersiz model olcumu')
    path = home / 'zincir/model-olcumleri.json'
    with FileLock(path.with_suffix('.lock')):
        rows = _kayitlar(home)
        rows.append({'model': kanonik_model(hedef), 'yol': hedef, 'rol': rol, 'dogru': dogru,
                     'toplam': toplam, 'saniye': saniye, 'ts': time.time() if simdi is None else simdi})
        atomic_json(path, {'surum': 1, 'olcumler': rows[-500:]})


def olcumler(home: Path, rol: str, simdi: float) -> dict[str, ModelOlcumu]:
    groups: dict[str, list[dict]] = {}
    kind = 'hakem' if rol in ('review', 'kalite') else 'genel'
    for row in _kayitlar(home):
        try:
            if (row['rol'] != kind or not 0 <= simdi - float(row['ts']) <= OLCUM_OMRU
                    or type(row['toplam']) is not int or type(row['dogru']) is not int
                    or not 0 <= row['dogru'] <= row['toplam'] or row['toplam'] <= 0
                    or not math.isfinite(float(row['saniye'])) or row['saniye'] < 0
                    or not isinstance(row['model'], str)):
                continue
            groups.setdefault(row['model'], []).append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return {key: ModelOlcumu(key, sum(r['dogru'] for r in rows), sum(r['toplam'] for r in rows),
                            sum(r['saniye'] for r in rows)) for key, rows in groups.items()}


SONUC_OMRU = 30 * 86400


def sonuc_kaydet(home: Path, record: dict[str, Any], result: dict[str, Any], writers: list[str],
                 judge: str | None, planner: str | None, simdi: float | None = None) -> dict[str, Any] | None:
    """N-RT02 (CR-39): outcome measurement from the production acceptance path. The same run+product+verdict does not count as a second sample; the record
    does not change the selection policy; cost/time that cannot be measured stays 'unknown'."""
    status = str(result.get('status') or '')
    if status not in ('PASS', 'UYGULANDI', 'FAIL', 'ERROR', 'CANCELLED'):
        return None
    digest = str((result.get('revision') or {}).get('digest') or '')
    selection = record.get('model_selection') if isinstance(record.get('model_selection'), dict) else {}
    now = time.time() if simdi is None else simdi
    roles = [('impl', model) for model in sorted(set(writers)) if model]
    roles += [('kalite', judge)] if judge else []
    roles += [('plan', planner)] if planner else []
    cost: dict[str, Any] = {'kaynak': 'bilinmiyor', 'kesin_micro': None, 'askida_micro': None}
    ledger = home / 'zincir/request-budget.db'
    if ledger.is_file():
        try:
            import sqlite3
            from contextlib import closing
            from butce_kapisi import run_usage
            with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                settled, held = run_usage(con, str(record['run_id']), bounded=True)
            cost = {'kaynak': 'request-ledger', 'kesin_micro': settled, 'askida_micro': held}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            pass
    created = record.get('created')
    duration = (now - float(created)) if type(created) in (int, float) and 0 < created <= now else None
    try:
        from kosu import run_job_type
        job_type = run_job_type(record)
    except (ValueError, KeyError, TypeError):
        job_type = None
    identity = hashlib.sha256(json.dumps([record['run_id'], digest, status], sort_keys=True).encode()).hexdigest()
    efor = sorted({str(task['efor_politikasi'].get('politika')) for task in (record.get('tasks') or {}).values()
                   if isinstance(task, dict) and isinstance(task.get('efor_politikasi'), dict)}) or None
    entry = {'surum': 2, 'kimlik': identity, 'run_id': record['run_id'], 'proje_ust': record.get('proje_ust'),
             'is_turu': job_type, 'sonuc': status, 'neden': str(result.get('reason') or '')[:200] or None,
             'urun': digest or None, 'kaynak_sha': record.get('source_sha') or None,
             'roller': [{'rol': rol, 'yol': model, 'model': kanonik_model(model)} for rol, model in roles],
             'politika': selection.get('policy_version'), 'tercih': selection.get('preference'),
             'zorluk_tahmini': selection.get('difficulty_estimate'), 'zorluk_duzeltme': selection.get('difficulty_override'),
             'zorluk_etkin': selection.get('difficulty_effective'), 'efor': efor,
             'maliyet': cost, 'yas_sn': round(duration, 3) if duration is not None else None, 'ts': now}
    path = home / 'zincir/model-sonuclari.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path.with_suffix('.lock')):
        existing = set()
        try:
            for line in path.read_text().splitlines():
                try:
                    existing.add(json.loads(line).get('kimlik'))
                except ValueError:
                    continue
        except OSError:
            pass
        if identity in existing:
            return None
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
    return entry


def sonuc_kayitlari(home: Path) -> tuple[list[dict[str, Any]], int]:
    rows, broken = [], 0
    try:
        lines = (home / 'zincir/model-sonuclari.jsonl').read_text().splitlines()
    except OSError:
        return [], 0
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('surum') != 2 or not isinstance(row.get('roller'), list):
                raise ValueError('row')
            rows.append(row)
        except ValueError:
            broken += 1
    return rows, broken


def sonuc_ozeti(home: Path, simdi: float | None = None) -> dict[str, Any]:
    """Report of fresh-old-few-failed samples per role/model/policy; does not affect the selection order."""
    now = time.time() if simdi is None else simdi
    rows, broken = sonuc_kayitlari(home)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        ts = row.get('ts')
        fresh = type(ts) in (int, float) and 0 <= now - ts <= SONUC_OMRU
        efor = '+'.join(row['efor']) if isinstance(row.get('efor'), list) and row['efor'] else None
        for role in row['roller']:
            key = (str(role.get('rol')), str(role.get('model')), str(row.get('politika')), str(efor))
            group = groups.setdefault(key, {'rol': key[0], 'model': key[1], 'politika': key[2], 'efor': efor, 'ornek': 0, 'taze': 0, 'eski': 0,
                                            'kabul': 0, 'basarisiz': 0, 'maliyet_micro': 0, 'maliyet_bilinmeyen': 0,
                                            'yas_sn': 0.0, 'yas_bilinmeyen': 0})
            group['ornek'] += 1
            group['taze' if fresh else 'eski'] += 1
            group['kabul' if row.get('sonuc') in ('PASS', 'UYGULANDI') else 'basarisiz'] += 1
            cost = row.get('maliyet') or {}
            if cost.get('kaynak') == 'request-ledger' and type(cost.get('kesin_micro')) is int:
                group['maliyet_micro'] += cost['kesin_micro']
            else:
                group['maliyet_bilinmeyen'] += 1
            if type(row.get('yas_sn')) in (int, float):
                group['yas_sn'] += float(row['yas_sn'])
            else:
                group['yas_bilinmeyen'] += 1
    for group in groups.values():
        group['yetersiz'] = group['taze'] < 5
        group['belirsizlik'] = [n for n, ok in (('az-ornek', group['yetersiz']), ('eski-ornek', group['eski'] > 0),
                                                  ('maliyet-bilinmiyor', group['maliyet_bilinmeyen'] > 0),
                                                  ('yas-bilinmiyor', group['yas_bilinmeyen'] > 0)) if ok]
    return {'gruplar': sorted(groups.values(), key=lambda g: (g['rol'], g['model'], g['politika'], str(g['efor']))), 'bozuk': broken,
            'toplam': len(rows), 'politikalar': sorted({str(r.get('politika')) for r in rows}),
            'not': 'olcum secimi degistirmez; politika degisikligi acik yapilandirma kararidir'}


def arac_uygun(hedef: str) -> bool:
    sag, ad = hedef.split('/', 1)
    # An unknown catalogue does not invalidate the approved role table; an explicit rejection is filtered out.
    return (katalog_model(sag, ad) or {}).get('tool_call') is not False


def yol_maliyeti(hedef: str, token: int) -> float:
    sag, ad = hedef.split('/', 1)
    if sag == 'opencode-go':
        return 0.0  # GO1: the remaining subscription allowance is used; the window filter is in the router.
    costs = fiyat(sag, ad)
    if not costs:
        return math.inf
    try:
        input_rate, output_rate = float(costs['input']), float(costs['output'])
        if not all(math.isfinite(n) and n >= 0 for n in (input_rate, output_rate)):
            return math.inf
        return (max(1, token) * input_rate + 4096 * output_rate) / 1e6
    except (KeyError, TypeError, ValueError):
        return math.inf


def sirala(adaylar: list[str], tercih: list[str], rol: str, token: int, home: Path,
           simdi: float) -> tuple[list[str], list[str]]:
    """Cannot undo the existing health/budget filtering; only orders the accepted candidates."""
    groups: dict[str, list[str]] = {}
    neden = []
    for route in adaylar:
        if not arac_uygun(route):
            neden.append(f'{route}: arac cagrisi desteklenmiyor')
            continue
        groups.setdefault(kanonik_model(route), []).append(route)
    ranks = {}
    for entry in tercih:
        for route in entry.split('|'):
            ranks.setdefault(kanonik_model(route), len(ranks))
    keys = sorted(groups, key=lambda key: ranks.get(key, len(ranks)))
    measured = olcumler(home, rol, simdi)
    result = []
    for key in keys:
        routes = sorted(groups[key], key=lambda route: yol_maliyeti(route, token))
        result.extend(routes)
        if key in measured:
            value = measured[key]
            neden.append(f'{key}: olcum {value.dogru}/{value.toplam}, puan={value.puan:.3f}')
        neden.append(f'{key}: yol sirasi {", ".join(routes)}')
    return result, neden
