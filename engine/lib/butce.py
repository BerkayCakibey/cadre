#!/usr/bin/env python3
"""Provider budget observation (prints JSON): remaining share, ratio, windows; no daily split."""
# butce.py — provider budget observation (prints JSON): remaining share, ratio, window; NO daily split.
# Sources: Go window API + opencode DB (Go points, Σ formula), OpenRouter credit API, manually entered Zen balance, calendar.
import json, sqlite3, datetime, pathlib, re, urllib.parse, urllib.request, sys, calendar, os
import time as time_mod
import math
from decimal import Decimal
from contextlib import closing
from butce_kapisi import spending, spending_tokens, amount, GUNLUK_ISTEK, kalibrasyon_makbuzu
from claude_politikasi import tavan as claude_tavan, pay as claude_pay, last_reset as claude_last_reset, oturum_say, STALE_LIMIT_MINUTES, STALE_FRESH_MINUTES
from kosu import atomic_json

# Can be redirected with CADRE_HOME / OPENCODE_DB (like the other modules; tests use scratch)
QE = pathlib.Path(os.environ.get("CADRE_HOME", str(pathlib.Path.home() / ".local/share/cadre-engineering")))
DB = pathlib.Path(os.path.expanduser(os.environ.get("OPENCODE_DB", str(pathlib.Path.home() / ".local/share/opencode/opencode.db"))))
YURUTUCU_KIMLIK = pathlib.Path.home() / '.local/share/opencode/auth.json'


def or_anahtari() -> str:
    try:
        anahtar = json.loads(YURUTUCU_KIMLIK.read_text())['openrouter']['key']
        if isinstance(anahtar, str) and anahtar.strip():
            return anahtar.strip()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return (QE / "zincir/openrouter-anahtar").read_text().strip()


def sim_yuk():
    """SIMULATED LOAD: CADRE_SIM_YUK=<json> is added ON TOP of the real numbers:
    {"go_puan": monthly points, "zen_usd": $}. Undefined in production -> {}."""
    try:
        yol = os.environ.get("CADRE_SIM_YUK")
        if yol and pathlib.Path(yol).exists():
            d = json.loads(pathlib.Path(yol).read_text() or "{}")
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def ag_acik():
    """CADRE_AG_YOK=1 (isolated test home): no provider endpoint is contacted; the observation becomes 'unreachable'."""
    return os.environ.get('CADRE_AG_YOK') != '1'


def go_gozlem(now_ts, go_bas, sim):
    """4c-13: Go is one pool and its unit is points (1 point = 1% of the monthly pool). The gate is the API percentage; steps after the
    reading are added with the Σ formula. Without the API, the Σ formula; if the opencode DB cannot be read either, unknown (closed)."""
    import go_pencere
    try:
        pencereler = go_pencere.oku(QE)
    except Exception:
        pencereler = None
    sim_pencere = sim.get("pencere") if isinstance(sim, dict) else None   # §40: boundary scenarios simulate the percentage
    if isinstance(sim_pencere, dict):
        pencereler = {'gecmis': [], **(pencereler or {}),
                      **{k: v for k, v in sim_pencere.items() if k in go_pencere.PENCERELER}}
    kendi = go_pencere.kendi_yuzdeler(DB, now_ts, (pencereler or {}).get('resets'), go_bas)
    kaynak, sonra, ayrisma = 'api', 0.0, ''
    if isinstance(pencereler, dict) and any(p in pencereler for p in go_pencere.PENCERELER):
        api_ts = float(pencereler.get('ts') or now_ts)
        sonra = go_pencere.sigma(DB, api_ts, now_ts) if api_ts < now_ts else 0.0
        # S11: if the API and Σ diverge, the cautious one (the larger percentage) wins
        pencereler, ayrisma = go_pencere.capraz_kontrol(pencereler, (kendi or {}).get('monthly'))
    elif kendi is not None:
        pencereler, kaynak = {**kendi, 'resets': None, 'ts': now_ts, 'gecmis': []}, 'db'
    else:
        pencereler = None
    if pencereler is None or sonra is None:   # the wall is still read from the API; remaining unknown -> gate closed
        return {'db': 'ulasilamadi', 'kaynak': kaynak, 'birim': 'puan', 'ay_puan': None, 'kullanilabilir': 0.0,
                'pay': 0.0, 'oran': 0.0, 'kendi': kendi, 'pencereler': pencereler, 'pencere_ayrisma': ayrisma}
    sonra += float(sim.get("go_puan", 0) or 0) if isinstance(sim, dict) else 0.0
    kalan = go_pencere.kalan_puan(pencereler, sonra) or 0.0
    ay_puan = float(pencereler.get('monthly') or 0) + sonra
    pay = kalan + ay_puan
    return {'db': 'ok', 'kaynak': kaynak, 'birim': 'puan', 'ay_puan': round(ay_puan, 3), 'kullanilabilir': round(kalan, 3),
            'pay': round(pay, 3), 'oran': round(kalan / pay, 3) if pay > 0 else 0.0, 'puan_sonra': round(sonra, 4),
            'kendi': kendi, 'pencereler': pencereler, 'pencere_ayrisma': ayrisma}

OR_SAPMA_ESIK = 0.20


def _or_defter_usd():
    """OR-PIN: the total (USD) the ledger (request-budget.db) has SETTLED for OpenRouter."""
    try:
        db = QE / "zincir/request-budget.db"
        if not db.exists():
            return 0.0
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            toplam = con.execute("select coalesce(sum(actual),0) from reservation where provider='openrouter' and actual is not null "
                                 "and coalesce(receipt,'') not like 'hayalet:%' and coalesce(receipt,'') not like 'kurtarma:%'").fetchone()[0]
        return float(toplam) / 1e6
    except (OSError, sqlite3.Error):
        return None


def _or_defter_gozlemi(stamp: float) -> dict[str, object] | None:
    from butce_kapisi import or_makbuz_gozlemi
    db = QE / 'zincir/request-budget.db'
    try:
        if not db.exists():
            return or_makbuz_gozlemi(None, stamp)
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            return or_makbuz_gozlemi(con, stamp)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def _or_defter_eski_usd(sinir):
    try:
        db = QE / "zincir/request-budget.db"
        if not db.exists():
            return 0.0
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            toplam = con.execute("select coalesce(sum(actual),0) from reservation where provider='openrouter' and actual is not null "
                                 "and coalesce(receipt,'') not like 'hayalet:%' and coalesce(receipt,'') not like 'kurtarma:%' "
                                 "and created < ?", (sinir,)).fetchone()[0]
        return float(toplam) / 1e6
    except (OSError, sqlite3.Error):
        return None


def _or_tahmini_usd():
    try:
        db = QE / "zincir/request-budget.db"
        if not db.exists():
            return 0.0
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            toplam = con.execute("select coalesce(sum(actual),0) from reservation where provider='openrouter' and actual is not null "
                                 "and (receipt like 'hayalet:%' or receipt like 'kurtarma:%')").fetchone()[0]
        return float(toplam) / 1e6
    except (OSError, sqlite3.Error):
        return 0.0


OR_SAPMA_FARK_USD = 0.50   # the ratio alone is not enough; on small amounts 20% is noise
OR_SAPMA_GECIKME_SN = 3600


def _or_sapma(total_usage):
    """OR-PIN: returns the drift if the spending growth in the credit API diverges from the ledger growth by more than 20% and at least $0.50, else None.
    Measurement starts from the first observation (baseline); a rollback renews the baseline, `cadre kurtar --ariza-temizle openrouter` resets it."""
    defter = _or_defter_usd()
    if defter is None:
        return {'neden': 'defter-okunamadi', 'api_usd': total_usage}
    if not all(math.isfinite(value) and value >= 0 for value in (total_usage, defter)):
        return {'neden': 'gecersiz-olcum'}
    tahmini = _or_tahmini_usd()
    taban_dosya = QE / "zincir/openrouter-taban.json"
    try:
        taban = json.loads(taban_dosya.read_text())
        api0, defter0, tahmini0 = float(taban['api_usd']), float(taban['defter_usd']), float(taban.get('tahmini_usd', 0))
        if not all(math.isfinite(v) and v >= 0 for v in (api0, defter0, tahmini0)):
            raise ValueError('taban gecersiz')
    except (OSError, ValueError, TypeError, KeyError):
        api0 = defter0 = tahmini0 = None
    d_api, d_defter, d_tahmini = ((total_usage - api0, defter - defter0, tahmini - tahmini0) if api0 is not None
                                  else (None, None, None))
    if api0 is None or d_api < 0 or d_defter < 0 or d_tahmini < 0:
        atomic_json(taban_dosya, {"ts": time_mod.time(), "api_usd": total_usage, "defter_usd": defter, "tahmini_usd": tahmini})
        return None
    eski = _or_defter_eski_usd(time_mod.time() - OR_SAPMA_GECIKME_SN)
    if eski is None:
        return {'neden': 'defter-okunamadi', 'api_usd': total_usage}
    alt, ust = min(d_defter, max(0.0, eski - defter0)), d_defter + d_tahmini
    fark = 0.0 if alt <= d_api <= ust else (d_api - ust if d_api > ust else alt - d_api)
    oran = fark / max(d_api, ust, 1e-9)
    if oran <= OR_SAPMA_ESIK or fark < OR_SAPMA_FARK_USD:
        return None
    return {"api_usd": round(d_api, 4), "defter_usd": round(d_defter, 4), "tahmini_usd": round(d_tahmini, 4), "sapma": round(oran, 4)}


def _or_kapat(sapma):
    """OR-PIN: writes the drift to a persistent warning file (read by the watcher/morning summary)."""
    try:
        (QE / "zincir/openrouter-sapma.json").write_text(
            json.dumps({"ts": datetime.datetime.now().timestamp(), **sapma}, ensure_ascii=False))
    except OSError:
        pass


def _or_kural(ad, kural):
    """Normalizes a policy line. List: provider names (old format). Object: `kademeler` endpoint tags,
    `saglayicilar` names; if neither, every endpoint tag matching the rule (quantization + maximum price); for `kapali`
    weights quantization is not required."""
    if isinstance(kural, list):
        return {'kimlik': 'provider_name', 'izinli': list(kural), 'kapali': ad.startswith('openai/'), 'kuant': 'fp8', 'tavan': None}
    etiketli = 'kademeler' in kural
    izinli = kural.get('kademeler' if etiketli else 'saglayicilar')
    # OR `only/order` takes a provider slug or an endpoint tag (docs: "deepinfra/turbo"); not the display name ("Sail Research")
    return {'kimlik': 'tag' if etiketli or izinli is None else 'provider_name', 'izinli': izinli,
            'kapali': kural.get('kapali') is True, 'kuant': kural.get('kuantizasyon') or 'fp8', 'tavan': kural.get('max_price')}


def _or_uclari(ad, kural, key):
    """Endpoints matching the policy: {id: {input, output, params, max_cikti, uptime, kuantizasyon}} (USD / 1M tokens)."""
    k = _or_kural(ad, kural)
    request = urllib.request.Request(f"https://openrouter.ai/api/v1/models/{ad}/endpoints",
                                     headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.load(response)['data']
    uclar = {}
    for endpoint in data.get('endpoints', []):
        kimlik = endpoint.get(k['kimlik'])
        if not kimlik or (k['izinli'] is not None and kimlik not in k['izinli']):
            continue
        if not k['kapali'] and endpoint.get('quantization') != k['kuant']:
            continue
        price = endpoint.get('pricing') or {}
        try:
            girdi, cikti, onbellek = (float(price[a]) * 1e6 for a in ('prompt', 'completion', 'input_cache_read'))
        except (KeyError, ValueError, TypeError):
            continue
        if not (all(math.isfinite(v) for v in (girdi, cikti, onbellek)) and girdi > 0 and cikti > 0 and 0 <= onbellek < girdi):
            continue
        tavan = k['tavan']
        if tavan and not (girdi <= float(tavan['prompt']) * 1.000001 and cikti <= float(tavan['completion']) * 1.000001):
            continue   # on a tie the smaller share; expensive tiers (region, fast) are left out
        sinir = endpoint.get('max_completion_tokens')
        acik = [float(v) for v in (endpoint.get('uptime_last_30m'), endpoint.get('uptime_last_1d')) if isinstance(v, (int, float))]
        bilinen = endpoint.get('supported_parameters')
        ad = endpoint.get('provider_name')
        uc = {'input': girdi, 'output': cikti, 'kuantizasyon': endpoint.get('quantization'),   # no parameter list means unknown (None)
              'params': sorted({'max_tokens', 'max_completion_tokens'} & set(bilinen)) if isinstance(bilinen, list) else None,
              'max_cikti': sinir if isinstance(sinir, int) and sinir > 0 else None, 'uptime': min(acik) if acik else None,
              'onbellek': onbellek, 'ad': ad if isinstance(ad, str) and ad else None}
        if kimlik in uclar:   # two endpoints with the same name (region copy): the worst value in every field
            o = uclar[kimlik]
            sinirlar = [v for v in (o['max_cikti'], uc['max_cikti']) if v]
            acik = [v for v in (o['uptime'], uc['uptime']) if v is not None]
            ortak = None if o['params'] is None or uc['params'] is None else sorted(set(o['params']) & set(uc['params']))
            uc = {**uc, 'input': max(o['input'], girdi), 'output': max(o['output'], cikti),
                  'params': ortak, 'max_cikti': min(sinirlar) if sinirlar else None,
                  'uptime': min(acik) if acik else None, 'onbellek': max(o['onbellek'], onbellek),
                  'ad': uc['ad'] if o['ad'] == uc['ad'] else None}
        uclar[kimlik] = uc
    taban = list(k['izinli']) if k['izinli'] is not None else list(uclar)
    from sinirlar import OR as OR_SINIR
    sira = sorted((u for u in taban if u in uclar), key=lambda u: (
        (uclar[u]['uptime'] if uclar[u]['uptime'] is not None else 100.0) < OR_SINIR['dusuk_uptime'],
        uclar[u]['input'] * 100_000 + uclar[u]['output'] * 32_768))   # default order; the plugin reorders with the request's own tokens
    return uclar, sira, k


def or_fiyat():
    """OR-PIN: only approved endpoints that report a cache price (fp8 required for open-weight models), below the maximum price.
    4c-7: if the refresh fails the last good price is kept (age-limited) and retried after 5 min; no empty file is written."""
    from sinirlar import OR as OR_SINIR
    if not ag_acik():
        return {}
    onb = QE / "zincir/openrouter-fiyat.json"
    simdi = time_mod.time()
    try:
        eski = json.loads(onb.read_text())
        eski = eski if eski.get('version') == 2 and eski.get('kimlik') == 'etiket' else {}
    except (OSError, ValueError, TypeError, AttributeError):
        eski = {}
    try:
        sonraki = min(float(eski.get('sonraki', 'inf')),
                      float(eski['ts']) + OR_SINIR['fiyat_taze_sn'] - OR_SINIR['fiyat_tekrar_sn'])
        if 0 <= simdi - float(eski['ts']) and simdi < sonraki:
            return eski.get("modeller", {})
    except (KeyError, ValueError, TypeError):
        pass
    modeller, basarisiz = {}, False

    def son_iyi(ad):
        onceki = (eski.get('modeller') or {}).get(ad)
        try:
            damga = float(onceki.get('fiyat_ts', eski['ts'])) if onceki else 0.0
            if onceki and 0 <= simdi - damga < OR_SINIR['fiyat_yas_sn']:
                modeller[ad] = {**onceki, 'fiyat_ts': damga}   # an old record without a stamp must not refresh its age as it is carried over
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    try:
        key = or_anahtari()
        policy = json.loads(pathlib.Path(__file__).with_name('openrouter-politika.json').read_text())
    except (OSError, ValueError, TypeError):
        key, policy, basarisiz = None, {}, True
        for ad in eski.get('modeller') or {}:
            son_iyi(ad)
    for ad, kural in policy.items():
        try:
            uclar, sira, k = _or_uclari(ad, kural, key)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            basarisiz = True
            son_iyi(ad)
            continue
        if sira:
            modeller[ad] = {'input': max(uclar[u]['input'] for u in sira), 'output': max(uclar[u]['output'] for u in sira),
                            'providers': sira, 'fp8_cache': True, 'kapali': k['kapali'],
                            'kademeler': {u: uclar[u] for u in sira}, 'fiyat_ts': simdi,
                            **({'max_price': k['tavan']} if k['tavan'] else {})}
    atomic_json(onb, {'version': 2, 'kimlik': 'etiket', 'ts': simdi, 'modeller': modeller,
                      'sonraki': simdi + (OR_SINIR['fiyat_tekrar_sn'] if basarisiz else OR_SINIR['fiyat_taze_sn'] - OR_SINIR['fiyat_tekrar_sn'])})
    return modeller


def _or_kalici_sapma():
    try:
        return json.loads((QE / "zincir/openrouter-sapma.json").read_text()).get('kalici') is True
    except (OSError, ValueError, AttributeError):
        return False


OR_MAKBUZ_ORNEK, OR_MAKBUZ_ARALIK, OR_MAKBUZ_KAPANIS = 3, 600, 2
OR_MAKBUZ_TOLERANS = (0.10, 0.002)   # ratio and minimum USD; rounding noise on small calls


def or_makbuz_denetimi():
    """4c-7(a): OR receipts in the ledger are sample-compared against OR's call breakdown (generation); on a mismatch a persistent
    drift is written and OR closes (`cadre kurtar --ariza-temizle openrouter` reopens it). Returns: mismatching samples or None."""
    if not ag_acik():
        return None
    kayit_yol = QE / "zincir/openrouter-makbuz-denetimi.json"
    try:
        kayit = json.loads(kayit_yol.read_text())
    except (OSError, ValueError):
        kayit = {}
    simdi = time_mod.time()
    if 0 <= simdi - float(kayit.get('ts', 0) or 0) < OR_MAKBUZ_ARALIK:
        return None
    denenen = list(kayit.get('denenen') or [])
    alt_sinir = simdi - 7 * 86400
    try:   # the period before the baseline (first observation / failure cleanup) was already judged (04.10 drift); only later is sampled
        alt_sinir = max(alt_sinir, float(json.loads((QE / "zincir/openrouter-taban.json").read_text())['ts']))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        key = or_anahtari()
        with closing(sqlite3.connect((QE / "zincir/request-budget.db").resolve().as_uri() + '?mode=ro', uri=True)) as con:
            satirlar = con.execute("select id, actual, receipt from reservation where provider='openrouter' and actual is not null "
                                   "and receipt like '%:gen-%' and created < ? and created > ? order by created desc limit 50",
                                   (simdi - 120, alt_sinir)).fetchall()
    except (OSError, sqlite3.Error):
        return None
    uymayan = []
    for ident, actual, receipt in [r for r in satirlar if r[0] not in denenen][:OR_MAKBUZ_ORNEK]:
        gen = receipt.split(':', 1)[1]
        try:
            istek = urllib.request.Request("https://openrouter.ai/api/v1/generation?id=" + urllib.parse.quote(gen),
                                           headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(istek, timeout=8) as response:
                dokum = float(json.load(response)['data']['total_cost'])
        except (OSError, ValueError, KeyError, TypeError):
            continue   # no breakdown yet or unreachable: retried later
        bizim = actual / 1e6
        if math.isfinite(dokum) and abs(dokum - bizim) > max(OR_MAKBUZ_TOLERANS[0] * max(dokum, bizim), OR_MAKBUZ_TOLERANS[1]):
            uymayan.append({'rezervasyon': ident, 'defter_usd': round(bizim, 6), 'dokum_usd': round(dokum, 6), 'ts': simdi})
        denenen.append(ident)
    toplam = [u for u in kayit.get('uymayan') or [] if float(u.get('ts') or 0) > alt_sinir] + uymayan
    atomic_json(kayit_yol, {'ts': simdi, 'denenen': denenen[-200:], 'uymayan': toplam[-20:]})
    if not uymayan:
        return None
    kapandi = len({u['rezervasyon'] for u in toplam}) >= OR_MAKBUZ_KAPANIS
    if kapandi:
        _or_kapat({'neden': 'makbuz-dokum', 'kalici': True, 'ornekler': toplam})
    return {'kapandi': kapandi, 'ornekler': uymayan}


def _deneme_bitti(meta):
    parcalar = [meta.get(k) for k in ('run_id', 'task_id', 'attempt_id')]
    if not all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', p) for p in parcalar):
        return False
    try:
        kayit = json.loads((QE / 'runs' / parcalar[0] / 'tasks' / parcalar[1] / 'attempts' / parcalar[2] / 'attempt.json').read_text())
    except (OSError, ValueError):
        return False
    return isinstance(kayit, dict) and kayit.get('outcome') in ('finished', 'not_started')


def or_askida_kapat():
    if not ag_acik():
        return 0
    db = QE / "zincir/request-budget.db"
    if not db.exists():
        return 0
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            satirlar = con.execute("select id, metadata from reservation where provider like 'openrouter%' and actual is null "
                                   "and json_valid(metadata) and json_extract(metadata,'$.generation') is not null and created < ?",
                                   (time_mod.time() - 20,)).fetchall()
        key = or_anahtari() if satirlar else ''
    except (OSError, sqlite3.Error):
        return 0
    from butce_kapisi import BudgetLedger, BudgetDenied
    from aylik_butce import MonthlyLedger, MonthlyBudgetDenied
    kapanan = 0
    for ident, meta in satirlar:
        try:
            meta = json.loads(meta)
        except ValueError:
            continue
        gen = meta.get('generation')
        if not isinstance(gen, str) or not re.fullmatch(r'gen-[A-Za-z0-9_-]{1,128}', gen) or not _deneme_bitti(meta):
            continue
        try:
            istek = urllib.request.Request("https://openrouter.ai/api/v1/generation?id=" + urllib.parse.quote(gen),
                                           headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(istek, timeout=8) as response:
                dokum = float(json.load(response)['data']['total_cost'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not math.isfinite(dokum) or dokum < 0:
            continue
        gercek = math.ceil(dokum * 1e6)
        try:
            BudgetLedger(db).settle(ident, gercek, f'{ident}:{gen}')
            aylik_id = meta.get('aylik_id')
            if isinstance(aylik_id, str) and aylik_id:
                MonthlyLedger(QE / 'zincir/aylik-butce.db', cap=1).settle(aylik_id, gercek, f'{aylik_id}:{gen}')
        except (BudgetDenied, MonthlyBudgetDenied, OSError, sqlite3.Error):
            continue
        kapanan += 1
    return kapanan


def or_bakiye():
    """03.10: OpenRouter prepaid credit (no daily split): remaining = total_credits - total_usage. 5 min cache, key
    zincir/openrouter-anahtar (600). Unreadable -> None -> gate closed (fail-closed). OR-PIN (04.10): if the credit API drop
    diverges from the ledger by more than 20%, OR closes + a warning is written; a persistent drift (receipt ↔ breakdown) stays closed until cleared manually."""
    if not ag_acik():
        return None
    if _or_kalici_sapma():
        return None
    onb = QE / "zincir/openrouter-kredi.json"
    try:
        d = json.loads(onb.read_text())
        if 0 <= datetime.datetime.now().timestamp() - float(d["ts"]) < 300:
            if d.get("kapali"):
                return None
            kalan = float(d['kalan'])
            return kalan if math.isfinite(kalan) and kalan >= 0 else None
    except Exception:
        pass
    try:
        gozlem = _or_defter_gozlemi(time_mod.time())
        key = or_anahtari()
        r = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(r, timeout=8) as response:
            v = json.load(response)['data']
        credits, usage = float(v['total_credits']), float(v['total_usage'])
        if not all(math.isfinite(value) and value >= 0 for value in (credits, usage)):
            return None
        kalan = float(max(Decimal(0), Decimal(str(v['total_credits'])) - Decimal(str(v['total_usage']))))
        sapma = _or_sapma(usage)
        if sapma is not None:
            _or_kapat(sapma)
            onb.write_text(json.dumps({"ts": datetime.datetime.now().timestamp(), "kalan": kalan,
                                       "kapali": True, "sapma": sapma}))
            return None
        (QE / "zincir/openrouter-sapma.json").unlink(missing_ok=True)   # 4c-7: drift is over; an old file does not show OR as failed
        if gozlem is not None:
            gozlem['harcanan_micro'] = int(Decimal(str(usage)) * 1_000_000)
        onb.write_text(json.dumps({"ts": datetime.datetime.now().timestamp(), "kalan": kalan, "yuklenen": credits,
                                  'kredi_gozlemi': gozlem}))
        return kalan
    except Exception:
        return None


def or_bedava_limiti():
    """D15: /key account counter; no new quota is created per model (UTC day)."""
    onb = QE / 'zincir/openrouter-hesap-limiti.json'
    now = time_mod.time()
    def dogrula(veri):
        sayim = veri['sayim']
        if any(type(sayim.get(k)) is not int or sayim[k] < 0 for k in ('used', 'limit', 'remaining')):
            raise ValueError('invalid free request count')
        if sayim['remaining'] > max(0, sayim['limit'] - sayim['used']):
            raise ValueError('inconsistent free request count')
        if not 0 <= now - float(veri['ts']) < 300 or int(now // 86400) != int(float(veri['ts']) // 86400):
            raise ValueError('stale account count')
        return sayim
    try:
        return dogrula(json.loads(onb.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not ag_acik():
        return None
    try:
        key = or_anahtari()
        request = urllib.request.Request('https://openrouter.ai/api/v1/key',
                                         headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=8) as response:
            sayim = json.load(response)['data']['free_model_daily_requests']
        veri = {'ts': now, 'sayim': sayim}
        dogrula(veri)
        from kosu import atomic_json
        atomic_json(onb, veri)
        return sayim
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _or_durum():
    """OR-PIN: observation dict of the 'or' bucket. If the credit API was unreachable or there is drift against the ledger
    the gate is closed; the drift also carries a human-readable warning. Also refreshes or_fiyat() (reservation file)."""
    kalan = or_bakiye()
    or_fiyat()
    net_micro = int(Decimal(str(kalan or 0)) * 1_000_000)
    net = net_micro / 1_000_000
    durum = {"db": "ok" if kalan is not None else "ulasilamadi", "kalan": net,
             "pay": net, "kullanilabilir": net}
    try:   # total credit loaded: the status screen shows the remaining ratio
        kredi = json.loads((QE / "zincir/openrouter-kredi.json").read_text())
        yuklenen = float(kredi.get("yuklenen") or 0)
        if kalan is not None and yuklenen > 0:
            durum["yuklenen"] = round(yuklenen, 4)
        if (kalan is not None and int(Decimal(str(kredi['kalan'])) * 1_000_000) == net_micro
                and isinstance(kredi.get('kredi_gozlemi'), dict)):
            durum['kredi_gozlemi'] = kredi['kredi_gozlemi']
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        pass
    if kalan is None:
        try:
            sapma = json.loads((QE / "zincir/openrouter-sapma.json").read_text())
            durum["db"] = "sapma"
            durum["uyari"] = (f"OR KAPALI: makbuz ↔ OR dokumu {len(sapma.get('ornekler') or [])} ornekte tutmadi"
                              if sapma.get('neden') == 'makbuz-dokum' else
                              f"OR KAPALI: kredi-api ${sapma.get('api_usd', 0):.2f} ile defter "
                              f"${sapma.get('defter_usd', 0):.2f} ayristi (%{sapma.get('sapma', 0) * 100:.0f})")
        except Exception:
            pass
    durum['bedava_istek'] = or_bedava_limiti()
    return durum


def _ay_ekle(dt, n):
    y, m = dt.year, dt.month + n
    y += (m - 1) // 12; m = (m - 1) % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def go_donem(now=None):
    """Go billing period. zincir/go-donem-reset.txt = 'YYYY-MM-DD HH:MM'.
    The current API takes precedence; without a local date the last fallback is the calendar month.
    A past local date is rolled forward month by month. Returns: (start_epoch, reset_dt|None, remaining_days)."""
    import math
    now = now or datetime.datetime.now()
    try:
        reset = datetime.datetime.strptime((QE / "zincir/go-donem-reset.txt").read_text().strip(), "%Y-%m-%d %H:%M")
    except Exception:
        reset = None
    try:   # B326: Go's own monthly reset is authoritative; when payment is late the period shifts and the date in the file goes stale
        import go_pencere
        r = ((go_pencere.oku(QE) or {}).get("resets") or {}).get("monthly")
        api_reset = datetime.datetime.fromisoformat(str(r).replace("Z", "+00:00")).astimezone().replace(tzinfo=None) if r else None
    except Exception:
        api_reset = None
    if api_reset is not None and api_reset > now:
        reset = api_reset
    if reset is None:
        ay_gun = calendar.monthrange(now.year, now.month)[1]
        bas = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(bas.timestamp()), None, max(1, ay_gun - now.day + 1)
    anchor, months = reset, 0
    while reset <= now:
        months += 1
        reset = _ay_ekle(anchor, months)
    bas = _ay_ekle(anchor, months - 1)
    return int(bas.timestamp()), reset, max(1, math.ceil((reset - now).total_seconds() / 86400))


IS_KOVASI_GUN = 7   # budget ladder: typical job cost from paid jobs of the last 7 days (p75)


def is_maliyeti_olc(simdi):
    """p75 of the cost per paid job (USD) for role bucket -> provider (at least 5 jobs) and '_' (all providers)."""
    import sqlite3
    from collections import defaultdict
    toplam = defaultdict(float)
    try:
        with closing(sqlite3.connect(f"file:{QE / 'zincir/request-budget.db'}?mode=ro", uri=True, timeout=5)) as con:
            for sag, actual, meta in con.execute('select provider, actual, metadata from reservation where actual is not null and created > ?',
                                                 (simdi - IS_KOVASI_GUN * 86400,)):
                m = json.loads(meta or '{}')
                if m.get('run_id'):
                    kova = 'plan' if m.get('task_id') == 'plan' else (m.get('dilim') or 'seritler')
                    toplam[(kova, sag, m['run_id'], m.get('task_id'))] += actual / 1e6
    except Exception:
        return {}
    gruplar = defaultdict(list)
    for (kova, sag, _r, _t), usd in toplam.items():
        if usd > 0.0005:
            gruplar[(kova, sag)].append(usd)
            gruplar[(kova, '_')].append(usd)
    sonuc = defaultdict(dict)
    for (kova, sag), v in gruplar.items():
        if len(v) >= 5:
            v.sort()
            sonuc[kova][sag] = round(v[min(len(v) - 1, int(0.75 * len(v)))], 4)
    return dict(sonuc)


def rezervasyon_olc(simdi):
    """B221: p50 of the reservation the gate holds per single call (USD, at least
    3 calls) for role bucket -> provider and '*' (provider-wide). Without it the ladder counts as 'active' a provider the gate would deny every time (R10-hard: OpenAI)."""
    import sqlite3
    from collections import defaultdict
    gruplar = defaultdict(list)
    try:
        with closing(sqlite3.connect(f"file:{QE / 'zincir/request-budget.db'}?mode=ro", uri=True, timeout=5)) as con:
            for sag, reserved, meta in con.execute('select provider, reserved, metadata from reservation where created > ?',
                                                   (simdi - IS_KOVASI_GUN * 86400,)):
                m = json.loads(meta or '{}')
                if m.get('run_id') and not m.get('gozlem'):
                    kova = 'plan' if m.get('task_id') == 'plan' else (m.get('dilim') or 'seritler')
                    gruplar[(kova, sag)].append(reserved / 1e6)
                    gruplar[('*', sag)].append(reserved / 1e6)
    except Exception:
        return {}
    sonuc = defaultdict(dict)
    for (kova, sag), v in gruplar.items():
        if len(v) >= 3:
            v.sort()
            orta = len(v) // 2
            sonuc[kova][sag] = round(v[orta] if len(v) % 2 else (v[orta - 1] + v[orta]) / 2, 4)
    return dict(sonuc)


KALIBRE_GUN, KALIBRE_EN_AZ, KALIBRE_CARPAN = 14, 5, 1.5


def kalibre_olc(simdi):
    """B253 (OBSERVATION-6): p95 of SETTLED receipts x1.5 for bucket -> provider = the gate's calibrated cap (USD). The worst case
    (output cap at full price + input 2x) came out at 37x reality for the judge, and sessions seemed not to fit their pocket. Pending, observation and
    receipts older than 14 days are not counted; a bucket with fewer than 5 receipts produces no value (the worst case remains)."""
    import sqlite3
    from collections import defaultdict
    gruplar = defaultdict(list)
    try:
        with closing(sqlite3.connect(f"file:{QE / 'zincir/request-budget.db'}?mode=ro", uri=True, timeout=5)) as con:
            for sag, actual, receipt, meta in con.execute('select provider, actual, receipt, metadata from reservation where created > ? and created <= ? and actual > 0',
                                                          (simdi - KALIBRE_GUN * 86400, simdi)):
                m = json.loads(meta or '{}')
                if m.get('run_id') and kalibrasyon_makbuzu(receipt, m):
                    kova = 'plan' if m.get('task_id') == 'plan' else (m.get('dilim') or 'seritler')
                    gruplar[(kova, sag)].append(actual / 1e6)
    except Exception:
        return {}
    sonuc = defaultdict(dict)
    for (kova, sag), v in gruplar.items():
        if len(v) >= KALIBRE_EN_AZ:
            v.sort()
            p95 = v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]
            sonuc[kova][sag] = round(p95 * KALIBRE_CARPAN, 4)
    return dict(sonuc)


def main():
    now = datetime.datetime.now()
    bugun = now.strftime("%Y-%m-%d")
    ay_gun = calendar.monthrange(now.year, now.month)[1]
    kalan_gun_takvim = max(1, ay_gun - now.day + 1)
    go_bas, go_reset, kalan_gun = go_donem(now)   # Go: panel reset period, not the calendar month
    SIM = sim_yuk()

    # --- Go: one pool, points (4c-13) + 429 stamp + window wall ---
    go = go_gozlem(now.timestamp(), go_bas, SIM)
    pencereler = go.pop('pencereler')
    pencere_acik, pencere_neden = True, "acik"
    try:
        t = int((QE / "zincir/go-429-son.txt").read_text().strip())
        if datetime.datetime.now().timestamp() - t < 18000:
            pencere_acik, pencere_neden = False, "429 damgasi (5 saat)"
    except Exception: pass
    try:
        import go_pencere
        acik, neden = go_pencere.durum(pencereler)
        if not acik:
            pencere_acik, pencere_neden = False, neden
    except Exception:
        pencereler = None

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # --- opencode zen (prepaid balance) --------------------------------------------------------
    # The balance cannot be read by a program: the source is the date+amount entered by the user, the baseline is the ENTRY TIME
    # (B92: taking the start of the day made a fresh balance shrink the share). It does not expire, so it is spread over a sliding horizon.
    zen_dosya = QE / "zincir/zen-bakiye.txt"       # format: "<YYYY-MM-DD[THH:MM:SS]> <balance-usd>"
    zen_giris, zen_tarih, zen_db = 0.0, None, 'yok'
    zen_taban_ts = 0.0
    try:
        zen_alanlar = zen_dosya.read_text().split()
        try:
            an = datetime.datetime.fromisoformat(zen_alanlar[0])
            zen_tarih = an.date()
            zen_taban_ts = an.timestamp() if an.time() != datetime.time() else 0.0
        except ValueError:
            zen_tarih = datetime.date.fromisoformat(zen_alanlar[0])
        zen_giris = float(zen_alanlar[1])
        zen_db = 'ok'
    except Exception:
        zen_giris, zen_tarih = 0.0, None
    if not zen_taban_ts:
        zen_taban_ts = (datetime.datetime.combine(zen_tarih, datetime.time()).timestamp() if zen_tarih else 0.0)
    zen_all = spending(DB, 'opencode', zen_taban_ts) if zen_db == 'ok' else None
    zen_day = spending(DB, 'opencode', day_start) if zen_db == 'ok' else None
    if zen_db == 'ok' and (zen_all is None or zen_day is None):
        zen_db = 'ulasilamadi'
    zen_harcanan = (zen_all[0] if zen_all else 0) + float(SIM.get("zen_usd", 0) or 0)
    zen_kalan = max(0.0, zen_giris - zen_harcanan) if zen_db == 'ok' else 0.0
    zen_bugun = (zen_day[0] if zen_day else 0) + float(SIM.get("zen_usd", 0) or 0)
    pay_zen = max(0.0, zen_kalan + zen_bugun) if zen_db == 'ok' else 0.0
    gun_kul_zen = max(0.0, pay_zen - zen_bugun)
    oran_zen = (gun_kul_zen / pay_zen) if pay_zen > 0 else 0.0


    # --- Groq (pre-filter only, 4c-11; direct-API counter from a file; limit from a single source) ---
    GROQ_LIMIT = GUNLUK_ISTEK['groq']
    gq = 0
    try:
        d_, v_ = (QE / "zincir/groq-gun-sayac.txt").read_text().split()
        if d_ == bugun: gq = int(v_)
    except Exception: pass
    gq_rows = [spending(DB, 'groq', day_start)]
    gq_db = 'ok' if all(r is not None for r in gq_rows) else 'ulasilamadi'
    gq += sum(r[1] for r in gq_rows if r is not None) if gq_db == 'ok' else GROQ_LIMIT
    oran_gq = max(0, GROQ_LIMIT - gq) / GROQ_LIMIT

    # --- Claude (subscription account; weekly reset rhythm; split by session count; own ledger when stale) ---
    vq = pathlib.Path.home() / ".claude/claude_quota.json"
    cl = {"kaynak": "yok", "pay": 0.0, "oturum": 0, "fable_bugun": 0}
    try:
        # B34: while the user is in Claude Code the status line may write the file AFTER `now` was taken -> negative age ->
        # `gecerli` False, share 0 (Claude silently closed for a minute). Negative age = fresh.
        yas_dk = max(0.0, float(os.environ.get("CADRE_TEST_STALE_DK") or (now.timestamp() - vq.stat().st_mtime) / 60))
        d7 = json.loads(vq.read_text())["rate_limits"]
        used7 = float(os.environ.get("CADRE_TEST_USED7") or d7["seven_day"]["used_percentage"]); used5 = float(os.environ.get("CADRE_TEST_USED5") or d7["five_hour"]["used_percentage"])
        tumj = json.loads(vq.read_text())
        ctx = (tumj.get("context_window") or {}).get("used_percentage")
        sess_cost = float((tumj.get("cost") or {}).get("total_cost_usd") or 0)
        # 1% <-> $ CALIBRATOR: learn a delta ratio from (used7, session cost) pairs
        kal = QE / "zincir/claude-kalibre.jsonl"
        try:
            son = json.loads(kal.read_text().splitlines()[-1]) if kal.exists() else None
        except Exception:
            son = None
        defter_kum = 0.0
        try:
            for _l in (QE / "zincir/claude-harcama.jsonl").read_text().splitlines():
                defter_kum += float(json.loads(_l).get("usd", 0) or 0)
        except Exception:
            pass
        if son is None or abs(used7 - son["used7"]) >= 1.0:
            with open(kal, "a") as f:
                f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "used7": used7, "cost": round(defter_kum, 4)}) + "\n")
        yuzde1 = None
        try:
            rows = [json.loads(l) for l in kal.read_text().splitlines()]
            # filter: used7 must grow (skip reset/account mix-ups), jump <=10 points,
            # cost must grow; cost = ENGINE ledger (not session cost) -> meaning "engine-$/1%"
            ciftler = [(b2["cost"] - a["cost"]) / (b2["used7"] - a["used7"])
                       for a, b2 in zip(rows, rows[1:])
                       if 0 < b2["used7"] - a["used7"] <= 10 and b2["cost"] > a["cost"]]
            if len(ciftler) < 3:
                ciftler = []
            if ciftler:
                ciftler.sort(); yuzde1 = round(ciftler[len(ciftler)//2], 1)
        except Exception: pass
        import subprocess as sp
        # B3: `pgrep -x claude` did not see the live session; counted by command name (basename).
        oturum = oturum_say(sp.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout.splitlines())
        # USD is a conservative conversion of the weekly reserve, not an
        # invented daily or monthly spending cap. An explicit existing profile
        # can seed calibration; absent both, paid admission remains unknown.
        if yuzde1 is None:
            try:
                yuzde1 = amount((QE/'zincir/claude-havuz-usd.txt').read_text().strip()) / 100
            except Exception:
                yuzde1 = None
        # C31/B2 — STALE OBSERVATION COMPENSATION: the observation refreshes only while the user is in Claude Code. If stale, the user
        # is not active; the only spending after that point is the automation's OWN ledger (usd / per-1% equivalent). If the observation
        # is from BEFORE the last weekly reset, the observed usage belongs to the previous week: only own spending after the reset counts.
        gozlem_ts = now.timestamp() - yas_dk * 60
        reset_ts = claude_last_reset(now.astimezone()).timestamp()
        onceki_hafta = gozlem_ts < reset_ts
        esik_ts = reset_ts if onceki_hafta else gozlem_ts
        defter = QE / "zincir/claude-harcama.jsonl"
        kendi_sonra = 0.0
        if defter.exists():
            for l in defter.read_text().splitlines():
                try:
                    r = json.loads(l)
                    r_ts = time_mod.mktime(time_mod.strptime(str(r["ts"])[:19], "%Y-%m-%dT%H:%M:%S"))
                    if r_ts > esik_ts:
                        if yuzde1:
                            kendi_sonra += float(r.get("usd", 0) or 0) / yuzde1
                        else:
                            kendi_sonra += float(r.get("yuzde_tahmin", 0.05) or 0.05)
                except Exception: pass
        used7_eff = (0.0 if onceki_hafta else used7) + kendi_sonra
        # P5: if the 5-hour window was full at observation time but `resets_at` has passed, the window is renewed; it does not stay 0 all night.
        reset5 = (d7.get("five_hour") or {}).get("resets_at")
        try:
            reset5_gecti = reset5 is not None and float(reset5) <= now.timestamp()
        except (TypeError, ValueError):
            reset5_gecti = False
        used5_eff = 0.0 if (onceki_hafta or reset5_gecti) else used5
        tavan = claude_tavan(now.astimezone())
        taze = yas_dk <= STALE_FRESH_MINUTES
        gecerli = 0 <= yas_dk <= STALE_LIMIT_MINUTES
        pay = claude_pay(used7_eff, used5_eff, now.astimezone(), oturum, stale_minutes=yas_dk)
        fb = 0
        fable_ids = []
        g = QE / "zincir/claude-harcama.jsonl"
        if g.exists():
            for l in g.read_text().splitlines():
                try:
                    r = json.loads(l)
                    if r["ts"][:10] == bugun and "fable" in r.get("model", ""):
                        fb += 1
                        if r.get('attempt_id'):
                            fable_ids.append(r['attempt_id'])
                except Exception: pass
        havuz_kalan = max(0.0, 100-used7_eff) * yuzde1 if yuzde1 is not None else 0.0
        kullanilabilir_usd = pay * yuzde1 if yuzde1 is not None else 0.0
        cl = {"kaynak": ("taze" if taze else f"bayat({int(yas_dk)}dk)"), "gecerli": gecerli,
              "gozlem_onceki_hafta": onceki_hafta, "reset5_gecti": reset5_gecti, "kendi_sonra": round(kendi_sonra, 2),
              "used7": round(used7_eff, 1), "used5": round(used5_eff, 1), "tavan": tavan, "oturum": oturum,
              "pay": round(pay, 1), "fable_bugun": fb, "fable_attempts": sorted(set(fable_ids)),
              "havuz_kalan_usd": round(havuz_kalan, 2), "kullanilabilir_usd": round(kullanilabilir_usd, 2),
              "ctx": ctx, "yuzde1_usd": yuzde1,
              "hafta_havuz_usd_tahmin": (round(yuzde1 * 100) if yuzde1 else None)}
    except Exception:
        pass

    print(json.dumps({
        "ts": now.isoformat(timespec="seconds"), "bugun": bugun, "kalan_gun": kalan_gun,
        "go": {**go, "donem_bas": datetime.datetime.fromtimestamp(go_bas).strftime("%Y-%m-%d %H:%M"),
               "reset": go_reset.strftime("%Y-%m-%d %H:%M") if go_reset else None, "kalan_gun": kalan_gun,
               "pencere_acik": pencere_acik, "pencere_neden": pencere_neden,
               "pencereler": ({k: pencereler.get(k) for k in ("rolling", "weekly", "monthly", "resets", "ts")}
                              if isinstance(pencereler, dict) else None)},
        "is_maliyeti": is_maliyeti_olc(now.timestamp()),   # budget ladder: role -> provider -> typical job (USD, p75)
        "rezervasyon": rezervasyon_olc(now.timestamp()),   # B221: role -> provider -> the gate's single-call reservation (USD, p50)
        "kalibre": kalibre_olc(now.timestamp()),           # B253: role -> provider -> calibrated cap (real p95 x1.5, USD)
        "zen": {"db": zen_db, "bakiye_giris": round(zen_giris, 3), "giris_tarihi": (zen_tarih.isoformat() if zen_tarih else None),
                "harcanan": round(zen_harcanan, 4), "kalan": round(zen_kalan, 3),
                "pay": round(pay_zen, 3), "kullanilabilir": round(gun_kul_zen, 3), "oran": round(oran_zen, 3)},
        "or": _or_durum(),
        # B106: `tpd` (daily token cap) is now read — on groq 200K TPD, 25 full-size calls with an 8K request.
        "groq": {"db":gq_db, "bugun_istek": gq, "oran": round(oran_gq, 3),
                 "bugun_token": spending_tokens(DB, 'groq', day_start)},
        "claude": cl,
        "sim": (SIM or None),
    }))

if __name__ == "__main__":
    main()
