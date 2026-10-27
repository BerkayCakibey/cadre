"""Cost and time breakdown per job (N-BU07/N-OBS02, CR-40). The ledger is the only source of money (no second ledger);
cash and subscription equivalents are summed separately; a value that cannot be measured stays unknown, never written as zero."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from kosu import RunStore, bounded_json, identifier

ASAMALAR = ('plan', 'plan-review', 'impl', 'qa', 'kalite', 'repair', 'yardimci', 'guvence', 'diger')
NAKIT = frozenset({'opencode', 'openrouter'})
ESDEGER = frozenset({'opencode-go', 'claude'})
HIZMET_UCRETI = 'ayri; defterde yok — kredi yukleme/BYOK/arac ucreti hesap kaydindadir, model harcamasina eklenmez'


def asama(meta: dict[str, Any]) -> str:
    task = str(meta.get('task_id') or meta.get('task') or '')
    role = str(meta.get('phase_role') or meta.get('role') or '')
    if meta.get('guvence') and meta.get('guvence_rol'):
        return 'guvence'
    if task.startswith('plan-review') or role == 'review':
        return 'plan-review'
    if task.startswith('plan') or role == 'plan':
        return 'plan'
    if task.startswith('duzeltme') or role in ('kurtarici', 'repair'):
        return 'repair'
    if task == 'qa' or role == 'qa':
        return 'qa'
    if task == 'kalite' or role == 'kalite':
        return 'kalite'
    if str(meta.get('dilim') or '') == 'yardimci' or role in ('scout', 'cadre-scout', 'compaction', 'otopsi', 'title', 'summary'):
        return 'yardimci'
    if role in ('impl', 'build', 'cadre-impl', 'birlestirme') or str(meta.get('dilim') or 'seritler') == 'seritler':
        return 'impl'
    return 'diger'


def _token_bos() -> dict[str, int]:
    return {'olculen_istek': 0, 'bilinmeyen_istek': 0, 'girdi': 0, 'cikti': 0, 'dusunme': 0, 'dusunme_bilinmeyen': 0,
            'onbellek_okuma': 0, 'onbellek_yazma': 0, 'onbellek_bilinmeyen': 0, 'onbellek_yazma_bilinmeyen': 0}


def _bos() -> dict[str, Any]:
    return {'nakit': {'kesin': 0, 'tahmini': 0, 'askida': 0}, 'esdeger': {'kesin': 0, 'tahmini': 0, 'askida': 0},
            'bilinmeyen_birim': 0, 'istek': 0, 'token': _token_bos()}


def _sayi(value: Any) -> bool:
    return type(value) is int and value >= 0


def token_ekle(hedef: dict[str, int], kullanim: Any) -> bool:
    """N-TS05 (CR-52): a request is measured only if the receipt's input+output were measured; thinking is inside the output, not added separately."""
    if not isinstance(kullanim, dict) or not (_sayi(kullanim.get('girdi')) and _sayi(kullanim.get('cikti'))):
        hedef['bilinmeyen_istek'] += 1
        return False
    hedef['olculen_istek'] += 1
    hedef['girdi'] += kullanim['girdi']
    hedef['cikti'] += kullanim['cikti']
    if _sayi(kullanim.get('dusunme')):
        hedef['dusunme'] += kullanim['dusunme']
    else:
        hedef['dusunme_bilinmeyen'] += 1
    if _sayi(kullanim.get('onbellek_okuma')):
        hedef['onbellek_okuma'] += kullanim['onbellek_okuma']
    else:
        hedef['onbellek_bilinmeyen'] += 1
    if _sayi(kullanim.get('onbellek_yazma')):
        hedef['onbellek_yazma'] += kullanim['onbellek_yazma']
    else:
        hedef['onbellek_yazma_bilinmeyen'] += 1
    return True


def aile(store: RunStore, run_id: str) -> list[str]:
    ids = {run_id}
    try:
        record = store.read(run_id)
        ids.update(str(x) for x in (record.get('project_run_ids') or []) if isinstance(x, str))
    except (OSError, ValueError, KeyError, TypeError):
        return sorted(ids)
    for candidate in store.list():
        if candidate.get('proje_ust') in ids:
            ids.add(str(candidate['run_id']))
    return sorted(ids)


def maliyet(home: Path, ids: list[str]) -> dict[str, Any]:
    path = home / 'zincir/request-budget.db'
    stages = {name: _bos() for name in ASAMALAR}
    if not path.is_file():
        return {'kaynak': 'bilinmiyor', 'asamalar': stages, 'bozuk': 0, 'not': 'istek defteri yok; tutar bilinmiyor'}
    marks = ','.join('?' * len(ids))
    uclar: dict[str, int] = {}
    onek = {'devam': 0, 'kirik': 0, 'ilk_veya_bilinmiyor': 0}
    yardimci: dict[str, int] = {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            rows = con.execute("select provider,reserved,actual,receipt,metadata from reservation where "
                               f"json_valid(metadata) and json_extract(metadata,'$.run_id') in ({marks})", ids).fetchall()
            broken = con.execute('select count(*) from reservation where not json_valid(metadata)').fetchone()[0]
    except sqlite3.Error:
        return {'kaynak': 'bilinmiyor', 'asamalar': stages, 'bozuk': 0, 'not': 'istek defteri okunamadi'}
    for provider, reserved, actual, receipt, metadata in rows:
        try:
            meta = json.loads(metadata)
            if type(reserved) is not int or (actual is not None and type(actual) is not int):
                raise ValueError('amount')
        except (ValueError, TypeError):
            broken += 1
            continue
        name = asama(meta)
        stage = stages[name]
        stage['istek'] += 1
        if name != 'guvence':
            token_ekle(stage['token'], meta.get('kullanim'))
            uc = (meta.get('kullanim') or {}).get('uc') if isinstance(meta.get('kullanim'), dict) else None
            uclar[uc if isinstance(uc, str) and uc else 'bilinmiyor'] = uclar.get(uc if isinstance(uc, str) and uc else 'bilinmiyor', 0) + 1
            devam = (meta.get('onek') or {}).get('devam') if isinstance(meta.get('onek'), dict) else None
            onek['devam' if devam is True else 'kirik' if devam is False else 'ilk_veya_bilinmiyor'] += 1
        if name == 'yardimci':
            role = str(meta.get('role') or meta.get('phase_role') or 'bilinmiyor')
            yardimci[role] = yardimci.get(role, 0) + 1
        pool = 'nakit' if provider in NAKIT else 'esdeger' if provider in ESDEGER else None
        if pool is None:
            stage['bilinmeyen_birim'] += 1
            continue
        if actual is None:
            stage[pool]['askida'] += reserved
        elif str(receipt or '').startswith(('hayalet:', 'kurtarma:')):
            stage[pool]['tahmini'] += actual
        else:
            stage[pool]['kesin'] += actual
    return {'kaynak': 'request-ledger', 'asamalar': stages, 'bozuk': broken, 'satir': len(rows), 'uclar': uclar, 'onek': onek,
            'yardimci_cagrilar': dict(sorted(yardimci.items()))}


def token_ozeti(cost: dict[str, Any]) -> Any:
    toplam = _token_bos()
    for stage in cost['asamalar'].values():
        for key, value in stage['token'].items():
            toplam[key] += value
    if not toplam['olculen_istek']:
        return 'bilinmiyor'
    return {'kaynak': 'request-ledger kullanim', **toplam, 'uclar': cost.get('uclar') or {}, 'onek': cost.get('onek') or {},
            'not': 'dusunme cikti icindedir; onbellek okuma/yazma girdi icindedir; ucret makbuz tutaridir, tokenlardan yeniden toplanmaz'}


def sure(store: RunStore, ids: list[str], root_id: str | None = None) -> dict[str, Any]:
    root_id = root_id or ids[0]
    work: dict[str, float] = {}
    queue, waits, setup, unknown_attempts = None, None, None, 0
    first_attempt = None
    for rid in ids:
        try:
            record = store.read(rid)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        created = record.get('created') if type(record.get('created')) in (int, float) else None
        if isinstance(record.get('budget_waits'), list):
            waits = waits or 0.0
            for window in record['budget_waits']:
                if isinstance(window, dict) and type(window.get('ts')) in (int, float) and type(window.get('until')) in (int, float):
                    waits += max(0.0, float(window['until']) - float(window['ts']))
        marker = store.path(rid) / 'taban-qa.json'
        if marker.is_file():
            try:
                seconds = (bounded_json(marker).get('kurulum') or {}).get('seconds')
                if type(seconds) in (int, float):
                    setup = (setup or 0.0) + float(seconds)
            except (OSError, ValueError, TypeError):
                pass
        for path in sorted((store.path(rid) / 'tasks').glob('*/attempts/*/attempt.json')):
            try:
                attempt = bounded_json(path)
            except (OSError, ValueError):
                unknown_attempts += 1
                continue
            start, end = attempt.get('created'), attempt.get('finished')
            if type(start) not in (int, float):
                unknown_attempts += 1
                continue
            if rid == root_id and created is not None:
                first_attempt = start if first_attempt is None else min(first_attempt, start)
            if type(end) in (int, float) and end >= start:
                role = asama({'task_id': attempt.get('task_id'), 'role': attempt.get('role')})
                work[role] = work.get(role, 0.0) + float(end - start)
            else:
                unknown_attempts += 1
    try:
        root = store.read(root_id)
        created = root.get('created')
        if first_attempt is not None and type(created) in (int, float):
            queue = max(0.0, float(first_attempt) - float(created))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {'calisma_sn': {k: round(v, 3) for k, v in sorted(work.items())}, 'kuyruk_sn': round(queue, 3) if queue is not None else None,
            'kota_bekleme_sn': round(waits, 3) if waits is not None else None, 'kurulum_sn': round(setup, 3) if setup is not None else None,
            'belirsiz_deneme': unknown_attempts, 'not': 'bekleme suresi model yavasligi degildir; token/cache defterde yoksa bilinmiyor'}


def kirilim(home: Path, run_id: str) -> dict[str, Any]:
    store = RunStore(home)
    identifier(run_id)
    root = store.read(run_id)
    ids = aile(store, run_id)
    cost = maliyet(home, ids)
    def basarisiz(rid: str) -> bool:
        kayit = store.read(rid)
        durum = (kayit.get('final_result') or {}).get('status')
        return durum not in (None, 'PASS', 'UYGULANDI') or (durum is None and kayit.get('state') in ('failed', 'cancelled'))
    failed = [rid for rid in ids if rid != run_id and basarisiz(rid)]
    failed_cost = maliyet(home, failed) if failed else None
    result = (root.get('final_result') or {}).get('status')
    return {'surum': 1, 'run_id': run_id, 'kapsam': ids, 'kabul': {'durum': result or 'bilinmiyor', 'kabul_edildi': result == 'PASS'},
            'maliyet': cost, 'basarisiz_cocuk': {'kosular': failed, 'maliyet': failed_cost},
            'sure': sure(store, ids, run_id), 'token': token_ozeti(cost), 'abonelik_bedeli': 'ayri; defterde yok',
            'hizmet_ucreti': HIZMET_UCRETI, 'yardimci_cagrilar': cost.get('yardimci_cagrilar') or {},
            'mudahale': __import__('kosu').mudahale_ozeti(root), 'ts': time.time()}


def kampanya(home: Path, run_ids: list[str]) -> dict[str, Any]:
    """N-TS05 (CR-52, TSR-03): campaign total cost / accepted jobs; failed jobs and child costs stay in the total, without an acceptance the ratio is undefined.
    Without a readable ledger total and ratio are unknown; with a broken line or an unknown unit the scope is written as partial."""
    store = RunStore(home)
    for rid in run_ids:
        identifier(rid)
    aileler = {rid: set(aile(store, rid)) for rid in dict.fromkeys(run_ids)}
    isler = sorted(rid for rid in aileler if not any(rid in uyeler and diger != rid for diger, uyeler in aileler.items()))
    kapsam = sorted(set().union(*(aileler[rid] for rid in isler))) if isler else []
    cost = maliyet(home, kapsam) if kapsam else {'kaynak': 'bilinmiyor', 'asamalar': {}}
    bilinen = cost['kaynak'] == 'request-ledger'
    toplam = {'nakit': {'kesin': 0, 'tahmini': 0, 'askida': 0}, 'esdeger': {'kesin': 0, 'tahmini': 0, 'askida': 0},
              'bilinmeyen_birim': 0, 'istek': 0}
    for stage in cost['asamalar'].values():
        for pool in ('nakit', 'esdeger'):
            for key in ('kesin', 'tahmini', 'askida'):
                toplam[pool][key] += stage[pool][key]
        toplam['bilinmeyen_birim'] += stage['bilinmeyen_birim']
        toplam['istek'] += stage['istek']
    kabul = [rid for rid in isler if (store.read(rid).get('final_result') or {}).get('status') == 'PASS']
    eksik = {'bozuk_satir': cost.get('bozuk', 0), 'bilinmeyen_birim': toplam['bilinmeyen_birim']}
    kapsam_durumu = ('kismi' if any(eksik.values()) else 'tam') if bilinen else 'bilinmiyor'
    oran: Any = 'tanimsiz'
    if kabul and not bilinen:
        oran = 'bilinmiyor'
    elif kabul:
        oran = {f'{pool}_{key}_usd': round(toplam[pool][key] / 1e6 / len(kabul), 6) for pool in ('nakit', 'esdeger') for key in ('kesin', 'tahmini')}
        oran['askida_ayri_usd'] = round((toplam['nakit']['askida'] + toplam['esdeger']['askida']) / 1e6, 6)
        oran['kapsam'] = kapsam_durumu
    return {'surum': 1, 'isler': isler, 'kapsam': kapsam, 'kabul_edilen': len(kabul), 'basarisiz_isler': sorted(set(isler) - set(kabul)),
            'toplam': toplam if bilinen else 'bilinmiyor', 'kabul_basina': oran, 'kaynak': cost['kaynak'],
            'maliyet_kapsami': {'durum': kapsam_durumu, **eksik} if bilinen else {'durum': 'bilinmiyor', 'not': cost.get('not') or 'kapsam bos'},
            'token': token_ozeti(cost) if kapsam else 'bilinmiyor',
            'hizmet_ucreti': HIZMET_UCRETI, 'abonelik_bedeli': 'ayri; defterde yok',
            'not': 'nakit ve esdeger ayri; askidaki tutar orana katilmaz; bilinmeyen birim toplanmaz; kucuk ornek genel garanti degildir',
            'ts': time.time()}


def ozet_satirlari(data: dict[str, Any]) -> list[str]:
    """FIX-44: a value that cannot be measured stays 'unknown', never written as zero; labels are translated with t()."""
    from dil import t as cevir
    def t(metin: str, **degerler: object) -> str:
        return cevir(metin, **degerler).lower()
    lines = []
    bilinmiyor = t('bilinmiyor')
    for name, stage in data['maliyet']['asamalar'].items():
        if not stage['istek']:
            continue
        if stage['bilinmeyen_birim'] >= stage['istek']:
            lines.append(f"{name}: {t('{n} istek bilinmeyen birimde, tutar bilinmiyor', n=stage['istek'])}")
            continue
        cash, eq = stage['nakit'], stage['esdeger']
        birim = f" · {t('{n} istek bilinmeyen birimde', n=stage['bilinmeyen_birim'])}" if stage['bilinmeyen_birim'] else ''
        lines.append(f"{name}: {t('nakit')} ${cash['kesin'] / 1e6:.4f} {t('kesin')} / ${cash['tahmini'] / 1e6:.4f} {t('tahmini')} / ${cash['askida'] / 1e6:.4f} {t('askıda')}"
                     f" · {t('eşdeğer')} ${eq['kesin'] / 1e6:.4f} / ${eq['tahmini'] / 1e6:.4f} / ${eq['askida'] / 1e6:.4f} · {t('{n} istek', n=stage['istek'])}{birim}")
    token = data.get('token')
    if isinstance(token, dict):
        olculen = token['olculen_istek']
        toplam = olculen + token['bilinmeyen_istek']
        def alan(deger: int, eksik: int) -> str:
            if eksik >= olculen:
                return bilinmiyor
            return f"{deger} [{t('{a}/{b} istekte', a=olculen - eksik, b=olculen)}]" if eksik else str(deger)
        lines.append(f"{t('token')}: {t('{a}/{b} istekte ölçüldü', a=olculen, b=toplam)} · {t('girdi')} {token['girdi']}"
                     f" ({t('önbellek okuma')} {alan(token['onbellek_okuma'], token['onbellek_bilinmeyen'])},"
                     f" {t('yazma')} {alan(token['onbellek_yazma'], token.get('onbellek_yazma_bilinmeyen', olculen))})"
                     f" · {t('çıktı')} {token['cikti']} ({t('düşünme')} {alan(token['dusunme'], token['dusunme_bilinmeyen'])})")
    elif token == 'bilinmiyor' and any(stage['istek'] for stage in data['maliyet']['asamalar'].values()):
        lines.append(f"{t('token')}: {bilinmiyor}")
    s = data['sure']
    kota = f"{s['kota_bekleme_sn']:.0f}s" if s.get('kota_bekleme_sn') is not None else bilinmiyor
    lines.append(f"{t('süre')}: " + ', '.join(f'{k} {v:.0f}s' for k, v in s['calisma_sn'].items())
                 + f" · {t('kuyruk')} {s['kuyruk_sn'] if s['kuyruk_sn'] is not None else bilinmiyor}"
                 + f" · {t('kota bekleme')} {kota} · {t('kurulum')} {s['kurulum_sn'] if s['kurulum_sn'] is not None else bilinmiyor}")
    return lines


def main(argv: list[str]) -> int:
    kampanya_kipi = bool(argv) and argv[0] == '--kampanya'
    if (kampanya_kipi and len(argv) < 2) or (not kampanya_kipi and len(argv) != 1):
        print('kullanim: verim.py RUN_ID | verim.py --kampanya RUN_ID...', file=sys.stderr)
        return 2
    import os
    home = Path(os.environ.get('CADRE_HOME', str(Path.home() / '.local/share/cadre-engineering')))
    try:
        data = kampanya(home, argv[1:]) if kampanya_kipi else kirilim(home, argv[0])
        print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'verim okunamadi: {type(exc).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
