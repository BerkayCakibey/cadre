"""RCA code28/29: in a single-task code job QA runs from the project restriction; the plan reviewer sees the fixed QA configuration explicitly."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))


class SabitQaIncelemesiTests(unittest.TestCase):
    def test_plan_incelemecisi_kisittaki_gercek_qa_komutunu_sabit_yapilandirma_olarak_gorur(self):
        from test_uctan_uca import E2EFixture
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = [sys.executable, str(fixture.root / 'lib/kosu.py')]

        def cli(*args, data=None):
            return subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                  text=True, capture_output=True, env=fixture.env, timeout=60)

        raw = dict(submission_id='sabit-qa', project='e2e', target_path=str(fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain TAMAM',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        rid = json.loads(cli('request', data=raw).stdout)['run_id']
        proposal = json.loads(cli('request-proposal', rid).stdout)
        first = json.loads(cli('request-approve', rid, data=proposal).stdout)
        self.assertEqual(cli('request-approve', rid, first['confirmation'], data=proposal).returncode, 0)
        self.assertEqual(cli('request-plan', rid).returncode, 0)
        kisit = (fixture.home / 'projects/e2e/qa-komut').read_text().strip()
        inceleme = [c for c in fixture.calls() if c['role'] == 'plan-review']
        self.assertEqual(len(inceleme), 1)
        istem = inceleme[0]['args'][-1]
        soru = json.loads(istem.splitlines()[-1])
        self.assertEqual(soru['sabit_qa'], {'kip': 'tek-gorev', 'kaynak': 'onayli-istek', 'komut': kisit})
        self.assertIn('sabit_qa motorun', istem)
        self.assertFalse(any('qa_komut' in g for g in soru['plan']['gorevler']), 'plan alanina kopya beklenmez')
        metin = json.dumps(fixture.PROMPT, ensure_ascii=False)
        self.assertEqual(soru['original_request'], fixture.PROMPT)
        self.assertNotIn('prompt', soru['request'], 'istek metni incelemeciye yalniz original_request alaninda verilir')
        self.assertEqual(istem.count(metin), 1, 'ayni istek metni istemde iki kez tasinmaz')
        planci = [c for c in fixture.calls() if 'tahmin_tavani_usd' in c['args'][-1]]
        self.assertEqual(len(planci), 1)
        plan_istemi = planci[0]['args'][-1]
        plan_soru = json.JSONDecoder().raw_decode(plan_istemi[plan_istemi.index('{"request":'):])[0]
        self.assertEqual(plan_soru['original_request'], fixture.PROMPT)
        self.assertNotIn('prompt', plan_soru['request'])
        self.assertEqual(plan_istemi.count(metin), 1)


if __name__ == '__main__':
    unittest.main()
