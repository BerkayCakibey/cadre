"""Single source of output, deadline and lane-context limits; plugins read the same file through `sinirlar.mjs`."""
from __future__ import annotations

import json
import os
from pathlib import Path

_VERI = json.loads(Path(__file__).with_name('sinirlar.json').read_text(encoding='utf-8'))
CIKTI: dict[str, int] = {k: int(v) for k, v in _VERI['cikti'].items()}
SON_SURE: dict[str, int] = {k: int(v) for k, v in _VERI['son_sure_sn'].items()}
OR: dict[str, float] = {k: float(v) for k, v in _VERI['openrouter'].items()}   # 4c-7: price file freshness and endpoint order
EFOR: dict[str, str | None] = dict(_VERI.get('efor') or {})


def inceleme_politikasi(is_turu: str = 'review') -> dict:
    """Finite review limits are copied into the run at acceptance; the environment cannot widen them."""
    policy = json.loads(json.dumps(_VERI['inceleme']))
    if is_turu == 'code':
        # G-B5: a code plan review uses the fixed helper pocket; it is not split again like the report phases.
        kod = _VERI['inceleme_kod']
        policy['surum'] = kod['surum']
        policy['plan-review']['pay_yuzde'] = kod['plan_pay_yuzde']
    return policy


def cikti(rol: str) -> int:
    """The role's output cap (tokens); every role other than plan/kalite/qa gets the lane cap."""
    return CIKTI.get(rol, CIKTI['serit'])


def son_sure(rol: str) -> int:
    return SON_SURE[rol]


def motor_sureleri() -> tuple[int, int]:
    values = _VERI['motor_sure_sn']
    warn = int(os.environ.get('CADRE_WARN_SEC', str(values['uyari'])))
    abort = int(os.environ.get('CADRE_ABORT_SEC', str(values['son'])))
    if warn < 0 or abort <= 0:
        raise ValueError('motor duration must be finite and positive; warning may be zero')
    return min(warn, abort), abort


def serit_baglami(model_baglami: int) -> int:
    """The lane's effective context: min(upper bound, model context − margin)."""
    b = _VERI['baglam']
    return max(0, min(int(b['serit_tavan']), int(model_baglami) - int(b['serit_pay'])))
