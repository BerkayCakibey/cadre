"""QA-PIN: the approval of a code request carries the QA command as content; plan review, baseline preparation, QA and continue run the same command."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

YANLIS = 'touch YANLIS_QA_KOSTU && exit 3'


class OnayliQaKomutuUctanUcaTests(unittest.TestCase):
    def setUp(self):
        from test_uctan_uca import E2EFixture, FAKE_OPENCODE
        self.fixture = E2EFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        kapi = "if prompt.startswith('PLAN ISTEK UYUMU'):"
        self.assertEqual(FAKE_OPENCODE.count(kapi), 1)
        ek = ("if role == 'plan-review' and (home/'plan-review-kesik').exists():\n"
              "    (home/'plan-review-kesik').unlink(); sys.exit(1)\n")
        self.fixture.fake.write_text(FAKE_OPENCODE.replace(kapi, ek + kapi))
        self.ayar = self.fixture.home / 'projects/e2e/qa-komut'
        self.onayli = self.ayar.read_text().strip()
        self.command = [sys.executable, str(self.fixture.root / 'lib/kosu.py')]

    def cli(self, *args, data=None, timeout=60):
        return subprocess.run([*self.command, *args], input=json.dumps(data) if data is not None else None,
                              text=True, capture_output=True, env=self.fixture.env, timeout=timeout)

    def onayla(self, ad):
        raw = dict(submission_id=ad, project='e2e', target_path=str(self.fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=self.fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain TAMAM',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        submitted = self.cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = self.cli('request-proposal', rid)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        first = self.cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        return rid, proposal

    def kosulan_qa_komutlari(self, rid):
        checks = self.fixture.store.path(rid) / 'tasks/qa/checks'
        return [json.loads(p.read_text()).get('command') for p in sorted(checks.glob('*/result.json'))]

    def assert_onayli_komutla_bitti(self, rid):
        saved = self.fixture.store.read(rid)
        self.assertEqual((saved['state'], (saved.get('final_result') or {}).get('status')), ('completed', 'PASS'),
                         (saved.get('reason'), saved.get('qa')))
        self.assertEqual(Path(saved['constraints']['qa-komut']).read_text().strip(), self.onayli)
        self.assertEqual((saved['qa']['source'], saved['qa']['sha256']), ('approved', hashlib.sha256(self.onayli.encode()).hexdigest()))
        taban = json.loads((self.fixture.store.path(rid) / 'taban-qa.json').read_text())
        self.assertEqual(taban['qa']['command'], self.onayli, 'hazirlik onayli komutu kosar')
        komutlar = self.kosulan_qa_komutlari(rid)
        self.assertTrue(komutlar and all(self.onayli in k and 'YANLIS' not in k for k in komutlar), komutlar)
        self.assertFalse(list(Path(saved['worktree']).rglob('YANLIS_QA_KOSTU')), 'degisen proje ayari hic calismaz')

    def test_onay_komutu_icerikle_tasir_ve_sonradan_degisen_ayar_plan_incelemesine_hazirliga_qaya_gecmez(self):
        rid, proposal = self.onayla('qa-pin-normal')
        self.assertEqual(proposal['action']['qa_command'],
                         {'source': 'project', 'command': self.onayli, 'sha256': hashlib.sha256(self.onayli.encode()).hexdigest()})
        from arayuz_veri import istek_onay_metni
        self.assertIn(self.onayli, istek_onay_metni(rid, proposal), 'operator onaylayacagi komutu gorur')

        self.ayar.write_text(YANLIS + '\n')
        self.assertEqual(self.cli('request-plan', rid, timeout=120).returncode, 0)
        inceleme = [c for c in self.fixture.calls() if c['role'] == 'plan-review']
        soru = json.loads(inceleme[-1]['args'][-1].splitlines()[-1])
        self.assertEqual(soru['sabit_qa'], {'kip': 'tek-gorev', 'kaynak': 'onayli-istek', 'komut': self.onayli})

        executed = self.cli('run', rid, timeout=150)
        self.assertEqual(executed.returncode, 0, executed.stderr[-1500:])
        self.assert_onayli_komutla_bitti(rid)

    def test_yeniden_baslatmada_sabit_komut_kalir_ayar_degisikligi_kosuya_girmez(self):
        rid, _ = self.onayla('qa-pin-devam')
        (self.fixture.home / 'plan-review-kesik').touch()
        ilk = self.cli('run', rid, timeout=150)
        saved = self.fixture.store.read(rid)
        self.assertNotEqual(ilk.returncode, 0, saved.get('reason'))
        self.assertNotIn(saved['state'], ('completed', 'failed', 'cancelled'), saved.get('reason'))
        self.assertEqual(Path(saved['constraints']['qa-komut']).read_text().strip(), self.onayli, 'kaynakla birlikte sabitlendi')

        self.ayar.write_text(YANLIS + '\n')
        devam = self.cli('run', rid, timeout=150)
        self.assertEqual(devam.returncode, 0, (devam.stderr[-1500:], self.fixture.store.read(rid).get('reason')))
        self.assert_onayli_komutla_bitti(rid)


class KosuQaKomutuTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / 'projects/p').mkdir(parents=True)
        self.ayar = self.home / 'projects/p/qa-komut'
        self.ayar.write_text('pytest -q tests/a\n')
        self.kisit = self.home / 'kisit-qa-komut'
        self.kisit.write_text('pytest -q tests/a\n')
        sha = hashlib.sha256(b'pytest -q tests/a').hexdigest()
        self.kayit = {'project': 'p', 'source_sha': 'abc', 'constraints': {'qa-komut': str(self.kisit)},
                      'request_permission': {'status': 'approved', 'action': {
                          'qa_command': {'source': 'project', 'command': 'pytest -q tests/a', 'sha256': sha}}}}

    def test_sabit_kisit_ayar_degisse_de_onayli_komutu_verir(self):
        from kosu import kosu_qa_komutu
        self.ayar.write_text('exit 3\n')
        self.assertEqual(kosu_qa_komutu(self.home, self.kayit), ('pytest -q tests/a', 'onayli-istek'))

    def test_degisen_ya_da_kaybolan_kisit_proje_ayarina_dusmez(self):
        from kosu import kosu_qa_komutu
        self.kisit.write_text('exit 0\n')
        with self.assertRaisesRegex(ValueError, 'qa-command-changed'):
            kosu_qa_komutu(self.home, self.kayit)
        self.kisit.unlink()
        with self.assertRaisesRegex(ValueError, 'qa-command-changed'):
            kosu_qa_komutu(self.home, self.kayit)
        del self.kayit['constraints']['qa-komut']
        with self.assertRaisesRegex(ValueError, 'qa-command-changed'):
            kosu_qa_komutu(self.home, self.kayit)

    def test_sabitlenmeden_once_onayli_icerik_kullanilir(self):
        from kosu import kosu_qa_komutu
        self.kayit.update(source_sha='', constraints={})
        self.ayar.write_text('exit 3\n')
        self.assertEqual(kosu_qa_komutu(self.home, self.kayit), ('pytest -q tests/a', 'onayli-istek'))

    def test_onayinda_qa_komutu_olmayan_eski_kayit_onceki_yolu_korur(self):
        from kosu import kosu_qa_komutu
        del self.kayit['request_permission']['action']['qa_command']
        self.kisit.write_text('eski kisit\n')
        self.assertEqual(kosu_qa_komutu(self.home, self.kayit), ('eski kisit', 'kayit-kisiti'))
        self.kayit['constraints'] = {}
        self.assertEqual(kosu_qa_komutu(self.home, self.kayit), ('pytest -q tests/a', 'proje-ayari'))


if __name__ == '__main__':
    unittest.main()
