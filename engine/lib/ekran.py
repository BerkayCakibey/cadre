"""Shared screen rendering for menu, monitor and summary; no network calls."""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

SIMGE = {'running': '●', 'completed': '✔', 'failed': '✗', 'retryable': '⚑',
         'queued': '○', 'accepted': '○', 'cancelled': '⊘', 'uyari': '⚑',
         'gorev': '▶', 'izle': '▤', 'iptal': '⊘', 'ozet': '⌾', 'push': '↑',
         'merge': '⤴', 'kurtarma': '⚕', 'bakiye': '$', 'cik': '○',
         'dusunme': '✻', 'arac': '⏺', 'yama': '📝', 'sikistirma': '♻'}
DURUM = {'running': 'çalışıyor', 'completed': 'bitti', 'failed': 'durdu',
         'retryable': 'devam bekliyor', 'queued': 'sırada', 'accepted': 'hazırlanıyor',
         'cancelled': 'iptal edildi'}


def simge(ad: str) -> str:
    return SIMGE.get(ad, SIMGE['uyari'])


def temiz(metin: Any) -> str:
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(metin))
    return ''.join(c for c in text if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf'))


def genislik(metin: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in temiz(metin))


def sar(metin: str, sutun: int) -> list[str]:
    """Only wraps the screen; does not change the stored text of the task."""
    rows = []
    limit = max(2, sutun)
    for source in temiz(metin).expandtabs(4).split('\n'):
        line, width = '', 0
        for c in source:
            size = genislik(c)
            if line and width + size > limit:
                rows.append(line)
                line, width = '', 0
            line += c
            width += size
        rows.append(line)
    return rows


def para(deger: Any) -> str:
    try:
        value = float(deger)
        if math.isfinite(value) and value >= 0:
            return f'${value:.2f}'
    except (TypeError, ValueError):
        pass
    return 'ölçüm yok'


def kosu_ozeti(kayit: dict) -> str:
    state = kayit.get('state', '')
    # plan/QA/judge/correction steps are not plan parts; counting them made finished work show '2/5'
    tasks = [t for k, t in (kayit.get('tasks') or {}).items()
             if k not in ('plan', 'qa', 'kalite', 'birlestirme', 'main', 'helper') and not k.startswith('duzeltme')]
    done = sum(t.get('state') in ('completed', 'uretti', 'tamam') for t in tasks)
    progress = f'{done}/{len(tasks)} parça bitti' if tasks else DURUM.get(state, 'durum bilinmiyor')
    alarm = kayit.get('harcama_alarmi') or {}
    if alarm and float(alarm.get('ts') or 0) > float(kayit.get('harcama_onayi_ts') or 0):   # 4b: an alarm waiting for approval comes before everything
        status = f"⚠ harcama alarmı: {alarm.get('neden')} — onay: cadre harcama-onay {kayit.get('run_id', '')}".rstrip()
    elif state in ('failed', 'retryable', 'cancelled'):
        status = DURUM[state]
    elif kayit.get('denials') or kayit.get('budget_held_usd'):
        status = 'bir uyarı var'
    elif state == 'completed':
        status = 'sorun yok'
    else:
        status = DURUM.get(state, 'durum bilinmiyor')
    return f'{simge(state)} {progress} · {para(kayit.get("budget_spent_usd"))} harcandı' + ('' if status == progress else f' · {status}')


def butce_ozeti(butce: dict) -> str:
    go = butce.get('go') or {}
    go_metin = f"Go {float(go['kullanilabilir']):.0f} puan" if go.get('birim') == 'puan' and go.get('db') == 'ok' else 'Go ?'
    return ' · '.join([go_metin] + [f'{ad} {para((butce.get(key) or {}).get("kullanilabilir"))}'
                                    for ad, key in [('OR', 'or'), ('Zen', 'zen')]])


def arac_ozeti(data: dict) -> str:
    state = (data.get('state') or {}).get('status')
    if state == 'error':
        return f'{simge("failed")} araç hata verdi · ayrıntı: d'
    action = {'read': 'dosya okuma', 'glob': 'dosya arama', 'grep': 'metin arama',
              'bash': 'komut çalıştırma', 'edit': 'dosya düzenleme', 'write': 'dosya yazma',
              'apply_patch': 'yama uygulama', 'task': 'yardımcı görev', 'webfetch': 'sayfa okuma', 'websearch': 'web araması'}
    return f'{simge("completed" if state == "completed" else "arac")} ' + action.get(data.get('tool'), 'araç işlemi') + (
        ' tamamlandı' if state == 'completed' else 'sürüyor')


def fiyat_arizalari(home) -> list[str]:
    import sqlite3
    from contextlib import closing
    try:
        with closing(sqlite3.connect(f'file:{home / "zincir/request-budget.db"}?mode=ro', uri=True)) as con:
            return [str(r[0]) for r in con.execute('select provider from pricing_fault')]
    except sqlite3.Error:
        return []


def teslim_bekleyen(records: list) -> int:
    """Accepted runs not merged yet (same rule as cadre.sh teslim_adaylari)."""
    from pathlib import Path
    from sonuc import approved
    n = 0
    for x in records:
        # An old run whose worktree was deleted cannot be delivered; calling git for each one slowed down startup.
        if x.get('state') == 'completed' and x.get('worktree') and 'teslim_merge' not in x and Path(x['worktree']).is_dir():
            try:
                n += bool(approved(x))
            except Exception:
                pass
    return n


def butce_taze(home) -> dict:
    """06.10: the status screen showed "no measurement" while the 60 s cache was empty; butce.py runs once (at most 20 s)."""
    import json, os, subprocess, sys
    from pathlib import Path
    try:
        root = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))
        r = subprocess.run([sys.executable, str(root / 'lib/butce.py')], capture_output=True, text=True,
                           timeout=20, env={**os.environ, 'CADRE_HOME': str(home)})
        d = json.loads(r.stdout)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


GORUNUM_YAS_SN = 24 * 3600   # up to this age the status line shows the last measurement with its age; the decision gate takes its own fresh measurement


def arkada_tazele(home) -> None:
    """Refreshes the shared budget observation in the background; startup does not wait for the network."""
    import os, subprocess, sys
    from pathlib import Path
    lib = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1]))) / 'lib'
    kod = (f'import sys; sys.path.insert(0, {str(lib)!r}); from pathlib import Path; '
           f'from butce_kapisi import observed_budget; observed_budget(Path({str(home)!r}), Path({str(lib.parent)!r}))')
    try:
        subprocess.Popen([sys.executable, '-c', kod], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env={**os.environ, 'CADRE_HOME': str(home)})
    except OSError:
        pass


def butce_gorunumu(home, tazele: bool = True) -> tuple[dict, float]:
    """(budget, age_s): fresh cache, otherwise the last measurement + a background refresh; with no measurement at all, one synchronous measurement.
    `tazele=False`: only the cache is read, no measurement is started (read-only preview)."""
    import json, time
    try:
        saved = json.loads((home / 'zincir/request-budget.json').read_text())
        yas = time.time() - float(saved['observed'])
        if isinstance(saved['budget'], dict) and 0 <= yas < GORUNUM_YAS_SN:
            if yas >= 60 and tazele:
                arkada_tazele(home)
            return saved['budget'], yas
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return (butce_taze(home), 0.0) if tazele else ({}, 0.0)


def durum(proje: str) -> str:
    import os
    from pathlib import Path
    from kosu import RunStore
    from sabah_ozeti import bayat_rezervasyon
    home = Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))
    records = RunStore(home).list(proje or None)
    lines = [kosu_ozeti(records[-1]) if records else f'{simge("queued")} Henüz iş yok.']
    gorunum, yas = butce_gorunumu(home)
    lines.append(f'{simge("bakiye")} {butce_ozeti(gorunum)}' + (f' ({int(yas // 60)} dk önce ölçüldü)' if yas >= 60 else ''))
    stale, amount = bayat_rezervasyon(home)
    if stale:
        lines.append(f'{simge("uyari")} {stale} askıda rezervasyon ({para(amount)}) · cadre kurtar')
    ariza = fiyat_arizalari(home)
    if ariza:
        lines.append(f'{simge("uyari")} fiyat arızası, sağlayıcı kapalı: {", ".join(ariza)} · cadre kurtar --ariza-listele')
    teslim = teslim_bekleyen(records)
    if teslim:
        lines.append(f'{simge("push")} {teslim} bitmiş iş onay bekliyor · cadre push / cadre merge (her koşu ana daldan başlar)')
    lines.append('Ayrıntı: cadre durum --ayrinti · İzle: cadre izle')
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    if sys.argv[1] == 'simge':
        print(simge(sys.argv[2]))
    elif sys.argv[1] == 'durum':
        print(durum(sys.argv[2] if len(sys.argv) > 2 else ''))
