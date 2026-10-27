#!/usr/bin/env python3
"""Single router: produces a model/provider candidate chain per role, budget and context, for the engine and the dispatcher.
The chain may be empty (no cloud capacity); the caller handles it with a budget wait. Every decision is written to zincir/yonlendirici.jsonl.
Input: butce.py JSON, zincir/saglayici-durum.json (circuit breaker: 3 errors -> 30 min, 429/quota -> 60 min), role, job class, request size.
CLI:
  yonlendirici.py zincir <role> [class] [request_tokens]   -> JSON {"zincir":[...], "neden":[...]}
  yonlendirici.py ilk <role> [class] [request_tokens]      -> first model (rc 76 without capacity)
  yonlendirici.py sonraki <role> <current_model>           -> the next model after the current one in the chain
  yonlendirici.py ariza <provider|provider/model> [error]  -> write an error to the circuit breaker (at (model, route) level if a model is given)
  yonlendirici.py config <file>                            -> write OPENCODE_CONFIG for sub-agents + compaction
  yonlendirici.py kisa <short_name>                        -> plan JSON short name
  yonlendirici.py durum                                    -> circuit breaker table"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from kosu import FileLock, atomic_json

QE = pathlib.Path(os.environ.get("CADRE_HOME", os.path.expanduser("~/.local/share/cadre-engineering")))
# RCA: injection/regression tests (a fake opencode producing "quota") wrote failures to the LIVE circuit breaker -> DS and Go went into
# a fake 60 min cooldown. Tests redirect these two paths to their own scratch with CADRE_YON_DURUM / CADRE_YON_KAYIT.
DURUM = pathlib.Path(os.environ.get("CADRE_YON_DURUM", str(QE / "zincir" / "saglayici-durum.json")))
KAYIT = pathlib.Path(os.environ.get("CADRE_YON_KAYIT", str(QE / "zincir" / "yonlendirici.jsonl")))
# --- yetenek tablosu ------------------------------------------------------------------------------------
# context/istek_tavan are ONLY A FALLBACK (if the catalogue cannot be read); the real limit comes from the catalogue per MODEL.
# bucket: aylik|kredi|bedava|abonelik
SAGLAYICI = {
    "claude":      {"kova": "abonelik", "context": 200000, "istek_tavan": 150000},
    "opencode-go": {"kova": "aylik",    "context": 128000, "istek_tavan": 100000},
    "opencode":    {"kova": "kredi",    "context": 128000, "istek_tavan": 100000},   # 0c: zen (prepaid balance)
# Fallback values: the real limit comes from the catalogue per MODEL. On the free tier what cuts is TPM/RPM, not dollars.
    # 4c-11: groq only in the pre-filter (direct HTTP, plan_ds); no free provider in the chain. groq 18.09 (console.groq.com/docs/rate-limits + models): the values below are the FREE plan and
    # EXACTLY ours. The Developer plan gives 250K TPM / 1K RPM for gpt-oss-120b BUT is currently at FULL CAPACITY
    # (waiting list) — so it is not an option that can be bought. Cached tokens do not count towards the limit.
    "groq":        {"kova": "bedava",   "context": 128000, "istek_tavan": 5000,
                    "rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000,
                    "developer_tpm": 250000, "developer_durum": "kapasite dolu (bekleme listesi) 18.09"},
}

# MEANING of the request cap: 'oran' = a fraction of context (the cap grows with the context); 'mutlak' = a RATE limit
# (TPM/request), independent of context but never larger than the model's real context.
TAVAN_TIPI = {'openrouter': 'oran', 'claude': 'oran', 'opencode-go': 'oran', 'opencode': 'oran', 'groq': 'mutlak'}
TAVAN_ORAN = 0.78          # the rest of the context is kept for output + margin
TAVAN_UST = 400000         # policy cap: we do not send 780k tokens in one request to a 1M-context model


def istek_tavani(sag, model_ad=''):
    """Upper bound that can be sent in one request for this model. The fallback in the table without a catalogue."""
    taban = SAGLAYICI.get(sag, {}).get('istek_tavan', 10 ** 9)
    bg = None
    if model_ad:
        try:
            from katalog import baglam
            bg = baglam(sag, model_ad)
        except Exception:
            bg = None
    if not bg:
        return taban
    if TAVAN_TIPI.get(sag) == 'oran':
        # The model context is always the upper bound; the floor in the table applies only when there is NO catalogue (B110).
        deger = min(TAVAN_UST, int(bg * TAVAN_ORAN))
    else:
        deger = min(taban, bg)
    # B90: TPM is also the upper bound of a single request — sending a 25K request to a provider that gives 8K tokens a minute
    # returns "Request too large" and opencode compacts and retries, entering a LOOP (measured:
    # groq 238 KB of log in 17 minutes). Even if the context is large, a small per-minute budget is the limit.
    tpm = SAGLAYICI.get(sag, {}).get('tpm')
    return min(deger, int(tpm)) if tpm else deger


# --- role -> required STRENGTH (item 1): chains say "which model", this table says "WHY". --------------------
#   strength: high=plan/reasoning · medium=code/review · low=summary/search · context: the role's typical request size
#   frequency: how many times the role runs per run (cost multiplier)
ROL_GUC = {
    "plan":        {"guc": "yuksek", "baglam": 200000, "sik": 1,  "not": "en buyuk baglam; pahali modelde tutmak Go butcesini yer"},
    "kalite":      {"guc": "yuksek", "baglam": 56000,  "sik": 1,  "not": "son kalite kapisi; kucuk baglam, yuksek getiri; yerel son halka"},
    "impl":        {"guc": "orta",   "baglam": 60000,  "sik": 5,  "not": "serit basina; kod yazan rol"},
    "review":      {"guc": "orta",   "baglam": 40000,  "sik": 5,  "not": "serit basina; impl'den FARKLI aile olmali"},
    "impl-hafif":  {"guc": "dusuk",  "baglam": 20000,  "sik": 3,  "not": "kucuk duzeltmeler"},
    "birlestirme": {"guc": "dusuk",  "baglam": 30000,  "sik": 2,  "not": "serit ciktilarini birlestirir"},
    "kurtarici":   {"guc": "orta",   "baglam": 60000,  "sik": 1,  "not": "takilan isi kurtarir"},
}


# 4c-1: removed providers; no code path or constant carries them (test_kaldirilan_saglayicilar).
KALDIRILAN_SAGLAYICILAR = ("deepseek", "cerebras", "openai", "mistral", "google", "groqx")   # 4c-1, 4c-11

# --- role -> preference order ---
OR_SOL, ZEN_SOL = "openrouter/openai/gpt-6.1-sol", "opencode/gpt-6.1-sol"   # Zen Sol is the last link: only when OR is cooling down
KOD_ZINCIRI = ["opencode-go/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro", "opencode-go/glm-5.3", "openrouter/z-ai/glm-5.3"]
# DECISION (D13): model first, then the cheapest route (by abundance, P7). Free models stay only in the helper
# chain and DO NOT WRITE CODE; when Go is full the same model moves to the Zen/OR route. test: test_rol_guc.py
HAKEM_ZINCIRI = [OR_SOL, "opencode-go/glm-5.3", "openrouter/z-ai/glm-5.3", "opencode-go/qwen3.8-max", "openrouter/qwen/qwen3.8-max-0902",
                 ZEN_SOL, "opencode/glm-5.3", "opencode/qwen3.8-max"]
ROLLER = {
    # plan (4c-6): claude (quota rule) › Sol (OR) › glm › qwen (Go) › Zen Sol. glm ahead of qwen: equal intelligence, qwen writes cache on every
    # short call. Zen Sol only when OR Sol is cooling down (zincir()).
    "plan":        ["claude/opus#high|claude/sonnet#high", OR_SOL, "opencode-go/glm-5.3", "openrouter/z-ai/glm-5.3", "opencode-go/qwen3.8-max",
                    "openrouter/qwen/qwen3.8-max-0902", ZEN_SOL],
    # code roles (4c-17): DS-pro › glm-5.3 › qwen3.8-max, Go › OR within each model; on a model error the next model is used.
    "impl":        KOD_ZINCIRI,
    "impl-hafif":  KOD_ZINCIRI,
    # N-QA03/N-TS03: the route measured at the last acceptance is used; lack of capacity is not an automatic hand-off to an unmeasured model.
    # Plan review keeps its own pool; the final judge's independence comes from a clean read-only session.
    "review":      list(HAKEM_ZINCIRI),
    "kalite":      [OR_SOL],
    # 4c-9/10: no exploration agent, the lane's own model does the compaction — no separate scout/compaction chain.
    "birlestirme": KOD_ZINCIRI,
    "kurtarici":   KOD_ZINCIRI,
    "ana":         [],
}





# short names in the dispatcher plan JSON
KISA = {"ds-pro": "opencode-go/deepseek-v4-pro", "ds-pro-go": "opencode-go/deepseek-v4-pro",
        "auto": "", "ds-flash": "opencode-go/deepseek-v4-flash",
        "glm-flash": "opencode-go/glm-5.3-flash", "cl-son": "claude/sonnet#high", "cl-opus": "claude/opus#high", "cl-fable": "claude/fable#xhigh",
        "cl-opus-max": "claude/opus#max"}


def saglayici(model):
    return model.split("/", 1)[0] if model else ""


def butce_oku():
    """Item 8: when the tick dropped to 15 s this function started a butce.py PROCESS on every call. Now the
    `observed_budget` shared cache (60 s, on disk, shared by all executors) is tried first; if that fails, the old path."""
    # The shared cache is ONLY for real observation. If the caller injected a simulation/test environment (CADRE_SIM_YUK,
    # CADRE_TEST_USED7 ...) the cache does not see that environment and answers wrongly — in that case butce.py is run directly.
    SIM_ORTAMI = ("CADRE_SIM_YUK", "CADRE_TEST_USED7", "CADRE_TEST_USED5", "CADRE_TEST_STALE_DK")
    if not any(os.environ.get(k) for k in SIM_ORTAMI):
        try:
            from butce_kapisi import observed_budget
            kok = pathlib.Path(__file__).resolve().parent
            veri = observed_budget(QE, kok.parent)
            if isinstance(veri, dict):
                return veri
        except Exception:
            pass
    try:
        return json.loads(subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parent / "butce.py")], capture_output=True, text=True, timeout=30).stdout)
    except Exception:
        return {}


def durum_oku():
    try:
        return json.loads(DURUM.read_text())
    except Exception:
        return {}


def durum_yaz(d):
    atomic_json(DURUM, d)


def kayit(**k):
    try:
        if k.get('run_id'):
            from kosu import RunStore, revision_context
            try:
                k = {**revision_context(RunStore(QE).read(k['run_id']), k.get('gorev_id')), **k}
            except (OSError, ValueError, KeyError):
                pass
        k.setdefault('timestamp', time.time())
        KAYIT.parent.mkdir(parents=True, exist_ok=True)
        with KAYIT.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%FT%T"), **k}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def model_anahtari(sag, model):
    """Key of a (model, route) record: the full `provider/model` id (the effort suffix `#...` is dropped; no new separator)."""
    ad = str(model or "").split("#", 1)[0]
    return ad if ad.startswith(sag + "/") else f"{sag}/{ad}"


def uc_sogumalari(sag, model, d=None, simdi=None):
    """N-RT04: an outage of one known endpoint does not close the same model's other endpoints."""
    d = d if d is not None else durum_oku()
    simdi = time.time() if simdi is None else simdi
    uclar = (d.get(model_anahtari(sag, model)) or {}).get('uclar') or {}
    return {uc: float(kayit.get('cooldown_bitis') or 0) for uc, kayit in uclar.items()
            if float(kayit.get('cooldown_bitis') or 0) > simdi}


def cooldownda(sag, d=None, simdi=None, model=None):
    """Is the provider or (if a model is given) this route of that model in its cooldown period?"""
    d = d if d is not None else durum_oku(); simdi = simdi or time.time()
    if float(d.get(sag, {}).get("cooldown_bitis", 0)) > simdi:
        return True
    if model and float(d.get(model_anahtari(sag, model), {}).get("cooldown_bitis", 0)) > simdi:
        return True
    kapali = uc_sogumalari(sag, model, d, simdi) if model and sag == 'openrouter' else {}
    if kapali:
        try:
            ad = model_anahtari(sag, model).split('/', 1)[1]
            uclar = json.loads((QE / 'zincir/openrouter-fiyat.json').read_text())['modeller'][ad]['providers']
            return bool(uclar) and all(uc in kapali for uc in uclar)
        except (OSError, ValueError, KeyError, TypeError):
            return True
    return False


def ariza(sag, hata="", d=None, simdi=None, allowed_fails=3, model=None, *, uc=None):
    with FileLock(DURUM.with_suffix(".lock")):
        return _ariza_kilitli(sag, hata, d, simdi, allowed_fails, model, uc=uc)


# Account-level signal (4c-17): key/balance/monthly cap cut all models at once; the provider closes immediately.
HESAP_IMZALARI = ("unauthorized", "payment required", "insufficient balance", "insufficient funds",
                  "insufficient credit", "invalid api key", "invalid_api_key", "authentication", "monthly limit",
                  "monthly cap", "monthly spend", "monthly usage limit", "spend limit")
# 4c QA2: 401/402 is an account signature only in an HTTP context; "(401 s)" or "140100 tokens" do not count
HESAP_KODU = re.compile(r"(?:status(?:code)?|code|http|error)[\"'\s:=]*40[12]\b|\b40[12]\s+(?:unauthorized|payment|forbidden)")


def hata_sinifi(hata):
    h = (hata or "").lower()
    if any(k in h for k in HESAP_IMZALARI) or HESAP_KODU.search(h):
        return "hesap"
    # Error class separation (B110): a context error and a network outage are NOT QUOTA. Definite quota signatures are checked first.
    if re.search(r"\b429\b", h) or any(k in h for k in ("quota", "kota", "rate limit", "ratelimit", "tpm", "rpm",
                                                         "tokens per", "requests per", "daily limit", "per day")):
        return "kota"
    if any(k in h for k in ("context length", "context_length", "maximum context", "context window", "too large",
                            "too long", "too many tokens", "reduce the length")):
        return "baglam"
    if any(k in h for k in ("timeout", "timed out", "connection", "econnreset", "epipe", "fetch failed", "network",
                            "temporarily unavailable", "502", "503", "504")):
        return "ag"
    return "kota" if any(k in h for k in ("insufficient", "exceeded")) else "hata"


def _ariza_kilitli(sag, hata="", d=None, simdi=None, allowed_fails=3, model=None, *, uc=None):
    """Circuit breaker (4c-17): if the model is known, (model, route) closes first; the provider closes only when >=2 of its models fail or on an
    account-level signal. 429/quota -> 60 min immediately; other errors after `allowed_fails` in a row -> 30 min."""
    d = d if d is not None else durum_oku(); simdi = simdi or time.time()
    sinif = hata_sinifi(hata)
    anahtar = model_anahtari(sag, model) if model and sinif != "hesap" else sag
    s = d.setdefault(anahtar, {"hata": 0, "cooldown_bitis": 0, "son": ""})
    uc = uc if model and sag == 'openrouter' and sinif != 'hesap' else None
    if uc:
        s = s.setdefault('uclar', {}).setdefault(uc, {"hata": 0, "cooldown_bitis": 0, "son": ""})
    s["hata"] = int(s.get("hata", 0)) + 1
    s["son"] = f"{time.strftime('%F %T', time.localtime(simdi))} [{sinif}] {hata[:100]}"
    if sinif in ("kota", "hesap"):
        s["cooldown_bitis"] = simdi + 3600; s["hata"] = 0
    elif s["hata"] >= allowed_fails:
        s["cooldown_bitis"] = simdi + 1800; s["hata"] = 0
    if not uc and anahtar != sag and float(s.get("cooldown_bitis", 0)) > simdi:
        from katalog import kanonik_model
        dusen = {kanonik_model(k) for k, v in d.items() if k.startswith(sag + "/") and isinstance(v, dict)
                 and float(v.get("cooldown_bitis", 0)) > simdi}
        if len(dusen) >= 2:   # two different models failed on the same route: the problem is in the provider
            p = d.setdefault(sag, {"hata": 0, "cooldown_bitis": 0, "son": ""})
            p["cooldown_bitis"] = max(float(p.get("cooldown_bitis", 0)), simdi + 1800)
            p["son"] = f"{time.strftime('%F %T', time.localtime(simdi))} [model] {len(dusen)} model dustu"
    durum_yaz(d)
    kayit(olay="ariza", saglayici=sag, model=anahtar if anahtar != sag else None, uc=uc, hata=hata[:120], sinif=sinif,
          cooldown_bitis=s["cooldown_bitis"])
    return d


def basari(sag, d=None, model=None):
    """C21: a successful call RESETS the consecutive-error counter (provider and, if given, that (model, route)). Cooldown is not touched."""
    with FileLock(DURUM.with_suffix(".lock")):
        d = d if d is not None else durum_oku()
        degisti = False
        for anahtar in (sag, model_anahtari(sag, model) if model else None):
            s = d.get(anahtar) if anahtar else None
            if s and int(s.get("hata", 0)):
                s["hata"] = 0
                degisti = True
        if degisti:
            durum_yaz(d)
            kayit(olay="basari", saglayici=sag, model=model_anahtari(sag, model) if model else None)
        return d


GOZLEM_ANAHTARI = {'groq': 'groq', 'opencode-go': 'go', 'opencode': 'zen', 'openrouter': 'or'}


def gunluk_token_kalan(sag, b):
    """B106: the `tpd` column was in the table but read in NO decision. For a provider with a daily token cap,
    the tokens left today; None without an observation (the rule is not applied, not fail-open — just no information)."""
    tpd = SAGLAYICI.get(sag, {}).get('tpd')
    if not tpd or not isinstance(b, dict):
        return None
    satir = b.get(GOZLEM_ANAHTARI.get(sag, sag)) or {}
    kullanilan = satir.get('bugun_token')
    if not isinstance(kullanilan, (int, float)):
        return None
    return max(0, int(tpd) - int(kullanilan))


def butce_var(sag, model, b, istek_token=8000, rol=None, salt_okur=False, selection=None):
    """Does the provider have budget/capacity for this request? (b: butce.py JSON)
    B112: `rol` is required information; the reservation at the gate is made from the role's own pocket."""
    if istek_token > istek_tavani(sag, model or ''):
        return False, "istek tavani"
    kalan_tpd = gunluk_token_kalan(sag, b)
    if kalan_tpd is not None and istek_token > kalan_tpd:
        return False, "gunluk token tavani"
    if not b:
        return False, "butce yok"
    try:
        from butce_kapisi import available_budget, model_kovasi, or_pin_fiyati
        from sinirlar import cikti as rol_ciktisi
        if sag == 'openrouter' and not model.endswith(':free') and not or_pin_fiyati(QE, model.split('/', 1)[-1],
                                                                                         rol_ciktisi(rol or 'serit')):
            return False, 'OR-PIN fiyati yok (eklenti reddeder)'   # smoke: the plan made an empty OR attempt on every run
        bucket = model_kovasi(sag, model.split('/', 1)[-1])
        required = 1
        if selection is not None and sag != 'claude':
            from model_secimi import cagri_tavani
            try:
                required = max(1, cagri_tavani(model, istek_token, rol_ciktisi(rol or 'serit'), QE))
            except ValueError as exc:
                return False, f'model katalogu/fiyat eksik ({exc}); ucretli yol kapali, kurulum-kontrol katalogu listeler'
        remaining = available_budget(QE, b, bucket, rol=rol, model=model.split('/', 1)[-1],
                                     gerekli=required, salt_okur=salt_okur)
        if remaining < required:
            return False, 'dogrulanmis pay yok'
        if sag == "claude":
            # C31: NO daily dollar cap; the remaining share (%) of the weekly reserve curve + the shared gate decide.
            cl = b.get("claude", {})
            pay = float(cl.get("pay", 0))
            if not pay > 0:
                return False, "claude payi yok"
            if "fable" in model:
                from claude_politikasi import fable_slots
                if fable_slots(QE, b) <= 0:
                    return False, "fable gunluk 2"
            return True, f"claude pay {pay}% (~${cl.get('kullanilabilir_usd', '?')})"
        if sag in ('opencode-go', 'opencode', 'openrouter'):
            return True, 'ortak kapida kullanilabilir pay var'
    except Exception as e:
        return False, f"butce okunamadi {e}"
    return False, "tanimsiz"


# D2: candidates in the SAME quality tier can replace each other; tiers are not skipped, free/claude
# are not ordered. The role's "strong" choice (qwen3.8-max plan, deepseek-v4-pro quality) is never reordered — quality is #1.
KALITE_KATI = {
    # D13: deepseek-v4-pro's routes are the same quality tier — ordered by abundance (P7); Go first within the model (model_secimi).
    "opencode-go/deepseek-v4-pro": 1, "openrouter/deepseek/deepseek-v4-pro": 1,
    "opencode-go/glm-5.2": 1, "opencode-go/glm-5.3-flash": 1,
    "opencode/deepseek-v4-flash": 1, "opencode-go/qwen3.8-flash": 1,
    "openrouter/deepseek/deepseek-v4-flash": 1,   # the same model via OR, $0.03/$0.06 (Zen $0.14/$0.28); abundance orders
}
ODENMIS = ("opencode-go", "opencode", "openrouter")


def bolluk(sag, b):
    """How much of the provider's share is free (butce.py's `kullanilabilir` / `pay` ratio). 0 if unknown.
    NOTE (T3): this measure is NOT the gate's own — the gate (`butce_kapisi.allowance`) subtracts a 15% margin. The purpose here is a rough
    "how free" signal for ordering; the admission decision always belongs to the gate."""
    try:
        anahtar = {"opencode-go": "go"}.get(sag)
        v = (b or {}).get(anahtar) or {}
        taban = float(v.get("pay") or 0)
        return (float(v.get("kullanilabilir") or 0) / taban) if taban > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


# T3 fix: the threshold was 1.0 — after the day's FIRST spending every provider became "tight" and the order flipped every day
# (the check ledger B4/B7/B9 caught this). Now only a provider that is REALLY about to run out (less than 25% of its share left)
# falls back.
SIKISIK_ESIK = 0.25

# Hysteresis: the order changes not on every call but when the difference is CLEAR and SUSTAINED. Otherwise the day's first spending
# could flip the order and change model mid-job (two halves of the same job split across two models).
HISTEREZIS_SN = float(os.environ.get("CADRE_SIRA_HISTEREZIS_SN", 300))
HISTEREZIS_FARK = float(os.environ.get("CADRE_SIRA_HISTEREZIS_FARK", 0.20))


def _sira_durumu(d):
    s = d.get("_sira")
    return s if isinstance(s, dict) else {}


def sikisik_mi(sag, b, d, simdi):
    """Should the provider fall back within its own quality tier? Measure: less than a quarter of its share left.
    Hysteresis: the settled decision changes only when the new trend has BOTH lasted HISTEREZIS_SN AND is clear."""
    bol = bolluk(sag, b)
    ham = bol < SIKISIK_ESIK
    durum = _sira_durumu(d)
    kayit = durum.get(sag)
    if not isinstance(kayit, dict):
        durum[sag] = {"sikisik": ham, "aday": ham, "ts": simdi}
        d["_sira"] = durum
        return ham
    if ham == bool(kayit.get("sikisik")):
        if kayit.get("aday") != ham:                       # the trend went back to the settled decision
            kayit.update(aday=ham, ts=simdi)
            d["_sira"] = durum
        return ham
    if kayit.get("aday") != ham:                           # a new trend started: the clock is set now
        kayit.update(aday=ham, ts=simdi)
        d["_sira"] = durum
        return bool(kayit.get("sikisik"))
    yeterli_sure = simdi - float(kayit.get("ts") or simdi) >= HISTEREZIS_SN
    belirgin = abs(bol - SIKISIK_ESIK) >= HISTEREZIS_FARK
    if yeterli_sure and belirgin:
        kayit.update(sikisik=ham, aday=ham, ts=simdi)
        d["_sira"] = durum
        return ham
    return bool(kayit.get("sikisik"))


def bolluga_gore_sirala(adaylar, b, d=None, simdi=None, rol="impl"):
    """G-E1/P7: order by net share / typical call cost within the same quality group.
    Without a verified net share the old tightness signal is the fallback; on a tie the hand-written order is kept."""
    sirali = list(adaylar)
    from butce_kapisi import BudgetDenied
    indeks = {m: i for i, m in enumerate(sirali)}
    katlar = {}
    for m in sirali:
        kat = KALITE_KATI.get(m)
        if kat is not None and saglayici(m) in ODENMIS:
            katlar.setdefault(kat, []).append(m)
    def anahtar(m):
        kat = KALITE_KATI.get(m)
        if saglayici(m) not in ODENMIS or kat is None or len(katlar.get(kat, [])) < 2:
            return (indeks[m], 0, indeks[m])                     # left untouched
        capa = min(indeks[k] for k in katlar[kat])
        sikisik = 1 if sikisik_mi(saglayici(m), b, durum, an) else 0
        return (capa, sikisik, indeks[m])
    durum = d if isinstance(d, dict) else {}
    an = simdi if simdi is not None else time.time()
    for modeller in katlar.values():
        yerler = [indeks[m] for m in modeller]
        puanlar = {}
        try:
            for m in modeller:
                sag = saglayici(m)
                maliyet = is_maliyeti(rol, sag, b)
                if not 0 < maliyet < float('inf'):
                    raise ValueError('gecersiz tipik maliyet')
                puanlar[m] = net_kapasite_usd(b, sag, m) / maliyet
            yeni = sorted(modeller, key=lambda m: -puanlar[m])
        except (BudgetDenied, ValueError, OSError):
            yeni = sorted(modeller, key=anahtar)
        for i, m in zip(yerler, yeni):
            sirali[i] = m
    return sirali


def rol_baglami(rol):
    """B121: the role's DECLARED typical request size. `ROL_GUC[rol]['baglam']` was in the table but
    read in no decision; call sites passed hand-written numbers (20,000 for compaction,
    while the declaration was 150,000). Now a caller that does not know uses this number."""
    return int((ROL_GUC.get(rol) or ROL_GUC.get("impl") or {}).get("baglam", 8000))


MERDIVEN_YEDEK, MERDIVEN_AKTIF = 0.5, 1.25   # budget QA (user decision): out if not enough for half a job, active if it covers a job comfortably
ROL_KOVASI = {"plan": "plan", "kalite": "hakem", "review": "yardimci"}
IS_MALIYETI_VARSAYILAN = {"plan": 0.03, "seritler": 0.06, "hakem": 0.07, "yardimci": 0.01}   # ledger p75; without a measurement


def is_maliyeti(rol, sag, b):
    """The role's typical job cost (USD): the butce.py ledger measurement (provider, otherwise all providers), otherwise the default."""
    kova = ROL_KOVASI.get(rol, "seritler")
    olcum = ((b or {}).get("is_maliyeti") or {}).get(kova) or {}
    try:
        return float(olcum.get(sag) or olcum.get("_") or IS_MALIYETI_VARSAYILAN[kova])
    except (TypeError, ValueError):
        return IS_MALIYETI_VARSAYILAN[kova]


def kapi_rezervasyonu(rol, sag, b):
    """B221: the typical reservation the gate holds for a single call on this provider (USD; role bucket, otherwise provider-wide); 0 without a measurement."""
    kova = ROL_KOVASI.get(rol, "seritler")
    k = ((b or {}).get("kalibre") or {}).get(kova) or {}
    r = (b or {}).get("rezervasyon") or {}
    try:
        if sag == "claude" and k.get(sag):   # B253: only the Claude limit holds with the calibrated cap; the other gates use the old rule
            return float(k[sag])
        return float(((r.get(kova) or {}).get(sag)) or (r.get("*") or {}).get(sag) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def saglayici_merdiveni(sag, rol, b, model=None):
    """'aktif' | 'yedek' | 'disarida'; None for a provider without a dollar share or with an unreadable budget."""
    from butce_kapisi import PAID, allowance
    if sag not in PAID:
        return None
    try:
        kalan = allowance(b, sag, model=model)[1] / 1_000_000
    except Exception:
        return None   # the chain's budget filter removes an unreadable budget
    rez = kapi_rezervasyonu(rol, sag, b)
    j = max(is_maliyeti(rol, sag, b), rez)
    if kalan < max(MERDIVEN_YEDEK * j, rez):   # B221: money that does not cover a single gate call is denied on every attempt
        return "disarida"
    return "yedek" if kalan < MERDIVEN_AKTIF * j else "aktif"


def merdiven(aday, rol, b):
    """Budget ladder: if the money that can be spent today is not enough for half a typical job in that role it is out (money accumulates), less than 1.25
    jobs it is a fallback, more is active. Free models and Claude are outside the rule (None); a paid model with unknown price counts (G-B8)."""
    sag = saglayici(aday)
    if not aday or sag == "claude":
        return None
    try:
        from katalog import bedava_mi
        if bedava_mi(sag, aday.split("/", 1)[1].split("#")[0]) is True:
            return None
    except Exception:
        pass
    return saglayici_merdiveni(sag, rol, b, aday.split("/", 1)[-1].split("#")[0])


def yazar_nefesi(aday, b, *, salt_okur=False):
    """B239: if a paid candidate's remaining share does not cover the gate reservation (p50) of two lane calls, returns (remaining, needed); if enough,
    free or unmeasured, None (the candidate stays)."""
    return _nefes(aday, b, "impl", salt_okur=salt_okur)


def hakem_nefesi(aday, b):
    """B242: the judge also makes at least two calls (tool + verdict); same measure with the judge bucket and the run's judge pocket."""
    return _nefes(aday, b, "kalite")


def kurtarici_nefesi(aday, b):
    """B277: the rescuer takes over the half-done SESSION; the share must cover at least CADRE_KURTARICI_NEFES (6) calls."""
    try:
        n = max(2, int(os.environ.get("CADRE_KURTARICI_NEFES", "6")))
    except ValueError:
        n = 6
    return _nefes(aday, b, "kurtarici", n)


def _nefes(aday, b, rol, cagri=2, *, salt_okur=False):
    from butce_kapisi import PAID
    sag = saglayici(aday)
    if not aday or (sag not in PAID and sag != "claude"):
        return None
    try:
        from katalog import bedava_mi
        if bedava_mi(sag, aday.split("/", 1)[1].split("#")[0]) is True:
            return None
    except Exception:
        pass
    rez = cep_rez = kapi_rezervasyonu(rol, sag, b)
    if sag == "openrouter":
        try:
            from sinirlar import CIKTI
            pin = json.loads((QE / "zincir/openrouter-fiyat.json").read_text())["modeller"][aday.split("/", 1)[1].split("#")[0]]
            rez = max(rez, (rol_baglami(rol) * float(pin["input"]) + CIKTI.get(rol, CIKTI["serit"]) * float(pin["output"])) / 1e6)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
    if rez <= 0:
        return None
    try:
        kalan = net_kapasite_usd(b, sag, aday)
    except Exception:
        return None
    mikro = lambda usd: round(usd * 1_000_000)   # floating point: 6 x 0.1 must not exceed 0.6 (an exactly sufficient share is enough)
    if mikro(kalan) < mikro(cagri * rez):
        return (kalan, cagri * rez)
    cep = kosu_cebi_kalan(rol, sag)   # B239b (EK3/R13-v4): even if the provider share is plentiful, if the RUN POCKET does not cover two calls the first call empties the pocket
    if cep is not None and mikro(cep) < mikro(cagri * cep_rez) and os.environ.get('CADRE_RUN_ID'):
        from butce_kapisi import available_budget
        cep = available_budget(QE, b, sag, rol=rol, gerekli=mikro(cagri * cep_rez), salt_okur=salt_okur) / 1e6
    if cep is not None and mikro(cep) < mikro(cagri * cep_rez):
        return (cep, cagri * cep_rez, "cep")
    return None


def net_kapasite_usd(b, sag, aday=""):
    """B289: breathing room is not the raw share but the ledger's net capacity (share - receipts - pending reservations of other runs).
    On Go points are converted to this model's list dollars (4c-13)."""
    from butce_kapisi import BudgetLedger
    model = aday.split("/", 1)[-1] if aday else None
    kalan, defter = allowance_usd(b, sag, model), QE / "zincir/request-budget.db"
    return min(kalan, BudgetLedger(defter).available(sag, b, model=model, salt_okur=True) / 1_000_000) if defter.exists() else kalan


def allowance_usd(b, sag, model=None):
    from butce_kapisi import allowance
    return allowance(b, sag, model=model)[1] / 1_000_000


def kosu_cebi_kalan(rol="impl", provider=""):
    """USD left in the active run's pocket for this role; None without a run or a cap."""
    rid = os.environ.get("CADRE_RUN_ID")
    if not rid:
        return None
    try:
        from butce_kapisi import run_remaining_usd
        return run_remaining_usd(QE, rid, rol, provider)
    except Exception:
        return None


def merdiven_sirasi(secilen, seviye, son_care):
    """Fallbacks go just before the last resort (the table's last link), at the very end if the last resort is not in the list."""
    yedek = [m for m in secilen if seviye.get(m) == "yedek"]
    digerleri = [m for m in secilen if seviye.get(m) != "yedek"]
    i = digerleri.index(son_care) if son_care in digerleri else len(digerleri)
    return digerleri[:i] + yedek + digerleri[i:]


def claude_kapali() -> bool:
    """06.10: if `CADRE_CLAUDE_PLAN=0` or the `zincir/claude-kapali` file exists, Claude enters no chain (quota protected)."""
    return os.environ.get("CADRE_CLAUDE_PLAN") == "0" or (QE / "zincir/claude-kapali").exists()


def rol_adaylari(rol):
    run_id = os.environ.get('CADRE_RUN_ID')
    if run_id:
        from kosu import RunStore, istek_kok_kimligi, request_authorized
        store = RunStore(QE)
        try:
            record = store.read(istek_kok_kimligi(store, run_id))
        except (OSError, ValueError, KeyError, TypeError):
            return []
        permission = record.get('request_permission') or {}
        if record.get('request') and permission.get('status') == 'approved':
            # N-IN01/N-RT01: a restart or a child process does not derive the approved pool
            # from the terminal's temporary chain or from the general default.
            if not request_authorized(record):
                return []
            key = {'yazar': 'plan', 'impl-hafif': 'impl'}.get(rol, rol)
            return list(permission['action']['models'].get(key, []))
    for anahtar in ((rol, "plan") if rol == "yazar" else (rol,)):
        ozel = [m.strip() for m in os.environ.get("CADRE_ZINCIR_" + anahtar.upper().replace("-", "_"), "").split(",") if m.strip()]
        if ozel:
            return ozel
    return ROLLER["plan"] if rol == "yazar" else ROLLER.get(rol, ROLLER["impl"])


def zincir(rol, sinif="orta", istek_token=None, b=None, d=None, simdi=None, kaydet=True):
    """Ordered model chain for the role; a provider that does not fit the request is removed (B120).
    `istek_token=None`: the caller does not know the real size, the role's declared typical size is used (B121).
    Returns (list, reasons)."""
    istek_token = rol_baglami(rol) if istek_token is None else istek_token
    b = b if b is not None else butce_oku(); d = d if d is not None else durum_oku(); simdi = simdi or time.time()
    adaylar = rol_adaylari(rol)   # B239: report writer = plan chain + breathing filter
    from model_secimi import kosu_secimi, politika_sirasi
    selection, policy_reason = None, []
    run_id = os.environ.get('CADRE_RUN_ID')
    if run_id:
        try:
            selection = kosu_secimi(QE, run_id)
            if selection is not None:
                adaylar, reason = politika_sirasi(adaylar, rol, selection)
                policy_reason.append(reason)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            reason = ['selection-policy-invalid: kayitli tercih/zorluk okunamadi']
            if kaydet:
                kayit(olay='zincir', rol=rol, sinif=sinif, istek_token=istek_token, zincir=[], neden=reason,
                      run_id=run_id, gorev_id=os.environ.get('CADRE_GOREV_ID') or None)
            return [], reason
    if claude_kapali():   # the user wanted to protect the Claude quota; the plan chain continues with Sol/Go/Zen
        adaylar = [m for m in adaylar if not m.startswith("claude/")]
    secilen, neden = [], policy_reason
    for aday in adaylar:
        if "|" in aday:  # claude: opus (remaining share of the weekly reserve >= 4 points) / sonnet — the same threshold as claude_politikasi.model
            opus, sonnet = aday.split("|")
            try:
                aday = opus if float((b or {}).get("claude", {}).get("pay", 0)) >= 4.0 else sonnet
            except (TypeError, ValueError):
                aday = sonnet
        sag = saglayici(aday)
        # 0c: the catalogue is a SUPERSET — putting a model the account cannot use into the chain means the call fails with 401
        # and wastes a link of the chain. If a live list exists we intersect with it, otherwise we leave it.
        try:
            from katalog import kullanilabilir
            if not kullanilabilir(sag, aday.split("/", 1)[1] if "/" in aday else aday):
                neden.append(f"{aday}: hesapta yok"); continue
        except Exception:
            pass
        if rol == "plan" and sinif in ("trivial", "kucuk") and sag == "claude":
            neden.append(f"{aday}: kucuk iste claude yakilmaz"); continue
        if cooldownda(sag, d, simdi):
            neden.append(f"{aday}: cooldown"); continue
        if cooldownda(sag, d, simdi, aday):
            neden.append(f"{aday}: model cooldown (bu yol hata verdi)"); continue
        if aday == ZEN_SOL and not (cooldownda("openrouter", d, simdi) or cooldownda("openrouter", d, simdi, OR_SOL)):
            neden.append(f"{aday}: Zen Sol yalniz OR Sol sogumadayken (4c-3)"); continue
        ok, why = butce_var(sag, aday, b, istek_token, rol=rol, salt_okur=not kaydet, selection=selection)   # B112: the pocket comes from the role
        if not ok:
            neden.append(f"{aday}: {why}"); continue
        secilen.append(aday); neden.append(f"{aday}: OK ({why})")
    _sira_once = json.dumps(d.get("_sira"), sort_keys=True) if isinstance(d, dict) else None
    secilen = bolluga_gore_sirala(secilen, b, d, simdi, rol=rol)
    seviye = {m: merdiven(m, rol, b) for m in secilen}   # last step: tier ordering does not undo the ladder
    neden += [f"{m}: merdiven — bugunku para bu roldeki yarim ise yetmiyor, birikiyor" for m in secilen if seviye[m] == "disarida"]
    secilen = merdiven_sirasi([m for m in secilen if seviye[m] != "disarida"], seviye, adaylar[-1] if adaylar else None)
    if rol == "yazar":   # B239 (R13-v4): the writer is a long lane; no attempt on a paid provider whose remaining share does not cover two lane calls
        kalanlar = []
        for m in secilen:
            nefes = yazar_nefesi(m, b, salt_okur=not kaydet)
            if nefes is None:
                kalanlar.append(m)
            else:
                kaynak = "kosu cebi" if len(nefes) > 2 else "kalan pay"
                neden.append(f"{m}: yazar — {kaynak} ${nefes[0]:.2f} iki serit cagrisini (${nefes[1]:.2f}) karsilamiyor")
        secilen = kalanlar
    # Item 7: if the hysteresis memory is not written to disk every call is the "first call" and hysteresis never works.
    if kaydet and isinstance(d, dict) and json.dumps(d.get("_sira"), sort_keys=True) != _sira_once:
        try:
            with FileLock(DURUM.with_suffix(".lock")):
                kalici = durum_oku()
                kalici["_sira"] = d.get("_sira")
                durum_yaz(kalici)
        except Exception:
            pass
    from model_secimi import sirala
    secilen, secim_nedeni = sirala(secilen, adaylar, rol, istek_token, QE, simdi)
    son_care = []
    for aday in reversed(adaylar):
        if saglayici(aday) != "opencode":
            break
        son_care.insert(0, aday)
    secilen = [m for m in secilen if m not in son_care] + [m for m in son_care if m in secilen]
    neden.extend(secim_nedeni)
    if kaydet:
        kayit(olay="zincir", rol=rol, sinif=sinif, istek_token=istek_token, zincir=secilen, neden=neden,  # B50: the decision is bound to the run/task, reasons are not dropped
              run_id=os.environ.get("CADRE_RUN_ID") or None, gorev_id=os.environ.get("CADRE_GOREV_ID") or None,
              selection=selection)
    return secilen, neden


BEKLEME_SEVIYELERI = ("zor", "cok-zor", "ultra")
BEKLEME_ESIK_SN = 7200   # design 3.3: wait if <=2 h remain until renewal, continue with what is available if longer


def guclu_mu(model):
    """Design 3.3: strong = a paid cloud model with budget (claude included); free models do not count as strong."""
    if not model:
        return False
    sag = saglayici(model)
    from butce_kapisi import PAID
    if sag != "claude" and sag not in PAID:
        return False
    try:
        from katalog import bedava_mi
        return bedava_mi(sag, model.split("/", 1)[1].split("#")[0]) is not True
    except Exception:
        return True


def yenilenmeye_kalan(simdi=None):
    """Seconds left until the local midnight when daily shares renew (the same local day as kova_gun_basi)."""
    from datetime import datetime, timedelta
    simdi = time.time() if simdi is None else simdi
    gece = datetime.fromtimestamp(simdi).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(0, int(gece.timestamp() - simdi))


def bulut_bekleme(rol, sinif, puan, simdi=None, esik=None):
    """Seconds to wait (0 = no wait): only at hard+ level, if the chain's first link is not strong and the renewal is near."""
    if esik is None:   # B268: CADRE_BEKLEME_ESIK_SN=0 disables waiting (e2e fixtures hung on the clock)
        try:
            esik = int(os.environ.get("CADRE_BEKLEME_ESIK_SN", BEKLEME_ESIK_SN))
        except ValueError:
            esik = BEKLEME_ESIK_SN
    from seviye import seviye
    if seviye(puan)["ad"] not in BEKLEME_SEVIYELERI:
        return 0
    z, _ = zincir(rol, sinif, 30000)
    if z and guclu_mu(z[0]):
        return 0
    kalan = yenilenmeye_kalan(simdi)
    return kalan + 60 if kalan <= esik else 0


def ayni_model_yollari(mevcut, rol="impl", istek_token=None, b=None, yasak=(), kendisi=False):
    """GO1/CANLI1: open routes that can carry the same session without changing model."""
    from katalog import kanonik_model
    kimlik = kanonik_model(mevcut)
    adaylar, _ = zincir(rol, istek_token=istek_token, b=b)
    return [m for m in adaylar if (kendisi or m != mevcut) and saglayici(m) not in yasak
            and not m.startswith("claude/")
            and kanonik_model(m) == kimlik]


def sonraki_model(mevcut, rol="impl", istek_token=None, b=None, yasak=()):
    """4c-17: if the same model has no open route, the first open route of another model waiting in the chain (filtered by (model, route))."""
    from katalog import kanonik_model
    kimlik = kanonik_model(mevcut)
    adaylar, _ = zincir(rol, istek_token=istek_token, b=b)
    return next((m for m in adaylar if saglayici(m) not in yasak and not m.startswith("claude/")
                 and kanonik_model(m) != kimlik), None)


def sonraki(rol, mevcut, **kw):
    z, _ = zincir(rol, **kw)
    if mevcut in z:
        i = z.index(mevcut)
        return z[i + 1] if i + 1 < len(z) else None
    return z[0] if z and z[0] != mevcut else None


def alternatifler(rol, mevcut, n=3, **kw):
    """B281/4c-17: the next links to try for a half lane, in chain order, at most n: excluding the failed (model, route); another
    model of the same provider is a candidate too (the provider leaves the chain only when >=2 of its models fail)."""
    if n <= 0:   # B298
        return []
    z, _ = zincir(rol, **kw)
    dusen = model_anahtari(saglayici(mevcut), mevcut) if mevcut else ""
    return [m for m in z if model_anahtari(saglayici(m), m) != dusen][:n]


# D13: expensive models are used only in the plan/judge role and at difficulty 5; at lower difficulty
# the chain is built with these candidates removed (a difficulty 2 job does not go to an expensive model).
PAHALI = {"openrouter/openai/gpt-6.1-sol", "openrouter/deepseek/deepseek-v4-pro",
          "opencode-go/deepseek-v4-pro", "opencode-go/qwen3.8-max"}


def zorluk_modeli(m0: str, zorluk: int, rol: str = "impl") -> str | None:
    """D13: difficulty 1-2 -> the impl-light chain, others the role chain; m0 is kept if it is in the chain, otherwise the first link
    (None = no capacity)."""
    hedef = "impl-hafif" if 1 <= zorluk <= 2 else rol
    z, _ = zincir(hedef, "orta", None)
    if not z:
        return None
    if os.environ.get('CADRE_RUN_ID'):
        from model_secimi import kosu_secimi
        try:
            if kosu_secimi(QE, os.environ['CADRE_RUN_ID']) is not None:
                return z[0]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
    return m0 if m0 in z else z[0]


def config_yaz(dosya, b=None, d=None, ek_izin=None):
    """OPENCODE_CONFIG for sub-agents (the first link of each role's chain); compaction is the lane's model (4c-10)."""
    cfg = {"$schema": "https://opencode.ai/config.json", "agent": {}}
    for ajan, rol in (("cadre-plan", "plan"), ("cadre-review", "review"), ("cadre-impl", "impl")):
        z, _ = zincir(rol, "orta", None, b, d)
        m = next((x for x in z if not x.startswith("claude/")), None)
        cfg["agent"][ajan] = {"model": m} if m else {"disable": True}
    if ek_izin:
        cfg["agent"]["build"] = {"permission": {"bash": ek_izin}}
    p = pathlib.Path(dosya); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=1))
    return str(p), cfg


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(2)
    cmd = a[0]
    if cmd == "zincir":
        z, n = zincir(a[1], a[2] if len(a) > 2 else "orta", int(a[3]) if len(a) > 3 else None)
        print(json.dumps({"zincir": z, "neden": n}, ensure_ascii=False))
    elif cmd == "ilk":
        z, _ = zincir(a[1], a[2] if len(a) > 2 else "orta", int(a[3]) if len(a) > 3 else None)
        if not z:
            print('no-capacity', file=sys.stderr); sys.exit(76)
        print(z[0])
    elif cmd == "alternatifler":
        print("\n".join(alternatifler(a[1], a[2], int(a[3]) if len(a) > 3 else 3)))
    elif cmd == "sonraki":
        m = sonraki(a[1], a[2])
        if m is None:
            print('no-capacity', file=sys.stderr); sys.exit(76)
        print(m)
    elif cmd == "bekleme":
        print(bulut_bekleme(a[1], a[2], a[3] if len(a) > 3 else 0))
    elif cmd == "ariza":   # if `provider/model` is given the record is at (model, route) level (4c-17)
        ariza(saglayici(a[1]), a[2] if len(a) > 2 else "", model=a[1] if "/" in a[1] else None); print("ok")
    elif cmd == "config":
        p, _ = config_yaz(a[1]); print(p)
    elif cmd == "kisa":
        print(KISA.get(a[1], ""))
    elif cmd == "uygun":
        # uygun <model|empty> <role> [difficulty]: move a model with budget/closed to the first link of the chain ('' = the router chooses).
        # S5: without a preflight the plan assigned tasks to a provider without budget; this check prevents that.
        m0 = a[1] if len(a) > 1 else ""; rol = a[2] if len(a) > 2 else "impl"
        zorluk = int(a[3]) if len(a) > 3 and a[3].isdigit() else 0
        m = zorluk_modeli(m0, zorluk, rol)
        if m is None:
            print('no-capacity', file=sys.stderr); sys.exit(76)
        print(m)
    elif cmd == "durum":
        print(json.dumps(durum_oku(), indent=1))
    else:
        print(__doc__); sys.exit(2)
