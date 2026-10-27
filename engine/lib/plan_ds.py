#!/usr/bin/env python3
"""Direct HTTP for small free helpers (pre-screening, canary); paid planning goes through the shared OpenCode adapter.
PROV/DM/PP/DUZ are the shell interface; import does no I/O."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.request
from typing import Any

from butce_kapisi import BudgetDenied, BudgetLedger, observed_budget, rol_butcesi
from kosu import atomic_json, atomic_write

QE = Path(os.environ.get('CADRE_HOME', str(Path.home()/'.local/share/cadre-engineering')))
CODE = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))
ENDPOINTS = {'groq':'https://api.groq.com/openai/v1/chat/completions'}


def credentials(provider: str) -> str:
    return json.loads((Path.home()/'.local/share/opencode/auth.json').read_text())[provider]['key']


def generate(provider: str, model: str, prompt: str, plain: bool = False, *, opener: Any = None) -> str:
    if provider not in ENDPOINTS:
        raise BudgetDenied('paid planning must use the guarded OpenCode adapter')
    ledger = BudgetLedger(QE/'zincir/request-budget.db')
    # this path made reservations without run_cap and without a pocket. Harmless today — ENDPOINTS only
    # covers groq, a FREE provider that counts requests, and the run cap looks at USD providers.
    # Still, the cap/pocket decision comes from one place: if a USD endpoint is added here the gate binds automatically.
    run_id = os.environ.get('CADRE_RUN_ID')
    tavan, dilim_adi = rol_butcesi(QE, run_id)
    ident = ledger.reserve(provider, 1, observed_budget(QE, CODE),
                           {'adapter':'direct', 'model':model, 'run_id':run_id,
                            'task_id':os.environ.get('CADRE_GOREV_ID') or 'helper'},
                           run_cap=tavan, dilim=dilim_adi)
    directory = QE/'direct-calls'/ident
    atomic_write(directory/'prompt.md', prompt)
    body: dict[str, Any] = {'model':model, 'messages':[{'role':'user','content':prompt}], 'max_tokens':4000}
    if not plain:
        body['response_format'] = {'type':'json_object'}
    if provider == 'groq':
        body['reasoning_format'] = 'hidden'
    request = urllib.request.Request(ENDPOINTS[provider], data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json', 'Authorization':'Bearer '+credentials(provider),
                 'User-Agent':'Mozilla/5.0 (Macintosh) opencode-cli'})
    # Every unknown/error outcome retains one request; retries reserve separately.
    with (opener or urllib.request.urlopen)(request, timeout=180) as response:
        receipt = json.load(response)
    atomic_json(directory/'response.json', receipt)
    ledger.settle(ident, 1, ident + ':' + str(receipt.get('id', 'complete-response')))
    return str(receipt['choices'][0]['message']['content'])


def main() -> int:
    try:
        print(generate(os.environ.get('PROV', 'groq'), os.environ['DM'], os.environ['PP'], os.environ.get('DUZ') == '1'))
        return 0
    except Exception as exc:
        # Never print HTTP request headers/credentials on provider failure.
        print('HATA: ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
