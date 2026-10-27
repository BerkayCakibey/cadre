#!/usr/bin/env python3
"""Shows the two pools of an opencode account on one screen: the Go subscription (three windows; unused allowance expires)
and Zen credit (prepaid; never expires, spent by `opencode/...` models). The two must not be mixed up."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from ekran import para

KOK = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))


def _butce() -> dict:
    ham = subprocess.run([sys.executable, str(KOK / 'lib/butce.py')],
                         capture_output=True, text=True, timeout=60).stdout
    return json.loads(ham) if ham.strip() else {}


def _cubuk(yuzde: float, genislik: int = 18) -> str:
    dolu = max(0, min(genislik, round(genislik * yuzde / 100)))
    return '█' * dolu + '·' * (genislik - dolu)


def ozet(b: dict | None = None) -> str:
    b = b if b is not None else _butce()
    go, zen = b.get('go') or {}, b.get('zen') or {}
    p = go.get('pencereler') or {}
    satir = []
    satir.append('OPENCODE — tek hesap, iki ayrı para')
    satir.append('')
    satir.append(f"  GO (abonelik · kullanılmayan YANAR · {go.get('kalan_gun', '?')} gün kaldı)")
    for ad, etiket in (('rolling', 'anlık '), ('weekly', 'haftalık'), ('monthly', 'aylık ')):
        deger = p.get(ad)
        if deger is None:
            continue
        satir.append(f"    {etiket} %{deger:>5.1f}  {_cubuk(float(deger))}")
    satir.append(f"    bu ay %{float(go.get('ay_puan') or 0):.1f} · duvara kalan {float(go.get('kullanilabilir') or 0):.1f} puan")
    satir.append(f"    pencere: {go.get('pencere_neden') or '?'}")
    satir.append('')
    satir.append('  KREDİ / ZEN (ön ödemeli · YANMAZ · `opencode/...` modelleri bunu harcar)')
    satir.append(f"    bakiye {para(zen.get('kalan'))} · harcanan {para(zen.get('harcanan'))}")
    satir.append(f"    kullanılabilir {para(zen.get('kullanilabilir'))}")
    satir.append('')
    satir.append('  NOT: panelde "One-time credits" KAPALI ise Go limiti dolunca kredi devreye GİRMEZ.')
    satir.append('       "Go bitti" ≠ "para bitti" — ikisi ayrı havuz.')
    return '\n'.join(satir)


if __name__ == '__main__':
    print(ozet())
