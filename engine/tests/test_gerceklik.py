"""4c-19: reality probes — an assumption deviation becomes a PROBLEM; no network and no money (fake API cache, fake opencode, temporary DB)."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import gerceklik  # noqa: E402


class GerceklikTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix='qe-gerceklik-')
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        (self.home / 'zincir').mkdir()

    def test_go_windows_are_compared_one_by_one_with_rounding(self):
        api = {'ts': time.time(), 'rolling': 3, 'weekly': 12, 'monthly': 27, 'resets': {}}
        with patch.object(gerceklik.go_pencere, '_onbellek', return_value=api), \
             patch.object(gerceklik.go_pencere, 'kendi_yuzdeler', return_value={'rolling': 4.9, 'weekly': 14.2, 'monthly': 25.8}):
            # the API truncates down: Σ − API ∈ [−1, 2] is accepted (rolling 1.9 passes); weekly +2.2 and monthly −1.2 are deviations
            self.assertEqual(gerceklik.go_pencereleri(self.home, self.home / 'db', 0),
                             ['go-pencere weekly: Σ %14.2 ≠ API %12', 'go-pencere monthly: Σ %25.8 ≠ API %27'])
        with patch.object(gerceklik.go_pencere, '_onbellek', return_value={}):
            self.assertEqual(gerceklik.go_pencereleri(self.home, self.home / 'db', 0), ['go-pencere: API gozlemi yok'])

    def test_every_chain_model_must_carry_its_role_output_and_or_needs_one_fitting_endpoint(self):
        katalog = {'opencode-go': {'models': {'glm-5.3': {'limit': {'output': 131072}}, 'kucuk': {'limit': {'output': 16384}}}}}
        fiyat = {'modeller': {'deepseek/deepseek-v4-pro': {'kademeler': {
            'DeepInfra': {'max_cikti': 16384, 'params': ['max_tokens']}, 'StreamLake': {'max_cikti': 384000, 'params': ['max_tokens']}}},
            'z-ai/glm-5.3': {'kademeler': {'Novita': {'max_cikti': 16384, 'params': ['max_tokens']}}}}}
        zincirler = {'impl': ['opencode-go/glm-5.3', 'openrouter/deepseek/deepseek-v4-pro', 'openrouter/z-ai/glm-5.3'],
                     'plan': ['claude/opus#high|claude/sonnet#high', 'opencode-go/kucuk', 'opencode-go/yok']}
        self.assertEqual(gerceklik.uc_sinirlari(fiyat, katalog, zincirler), [
            'uc impl: openrouter/z-ai/glm-5.3 hicbir izinli uc 32768 tokeni tasimiyor',
            'uc plan: opencode-go/kucuk cikti 16384 < 65536', 'uc plan: opencode-go/yok katalogda yok'])

    def test_sol_tiers_follow_the_policy_and_the_price_cap(self):
        politika = {'openai/gpt-6.1-sol': {'kapali': True, 'kademeler': ['openai/flex', 'openai', 'azure'],
                                           'max_price': {'prompt': 2, 'completion': 10}}}
        fiyat = {'modeller': {'openai/gpt-6.1-sol': {'kademeler': {
            'openai': {'input': 2, 'output': 10, 'params': ['max_tokens']},
            'azure': {'input': 2.2, 'output': 11, 'params': []}}}}}
        self.assertEqual(gerceklik.sol_kademeleri(fiyat, politika), [
            'sol: openai/gpt-6.1-sol kademe yok: openai/flex',
            'sol: openai/gpt-6.1-sol azure token parametresi bilinmiyor',
            'sol: openai/gpt-6.1-sol azure azami fiyatin ustunde'])

    def test_a_catalogue_price_change_is_reported_against_the_last_canary(self):
        katalog = {'opencode-go': {'models': {'glm-5.3': {'cost': {'input': 1.4, 'output': 4.4}}}}}
        zincirler = {'kalite': ['opencode-go/glm-5.3']}
        sorun, simdi = gerceklik.fiyatlar(self.home, katalog, zincirler)
        self.assertEqual(sorun, [])
        (self.home / 'zincir/gerceklik.json').write_text(json.dumps({'fiyatlar': simdi}))
        katalog['opencode-go']['models']['glm-5.3']['cost']['output'] = 5.5
        self.assertEqual(gerceklik.fiyatlar(self.home, katalog, zincirler)[0], ['fiyat: opencode-go/glm-5.3 1.4/4.4 -> 1.4/5.5'])

    def test_thinking_must_leave_text_in_the_opencode_database(self):
        db = self.home / 'opencode.db'
        with closing(sqlite3.connect(db)) as con:
            con.execute('create table message(id text, session_id text)')
            con.execute('create table part(id text, message_id text, data text)')
            con.execute("insert into message values('m1', 'ses_d')")
            con.execute("insert into part values('p1', 'm1', ?)", (json.dumps({'type': 'reasoning', 'text': 'dusundum'}),))
            con.commit()
        sahte = self.home / 'opencode'
        ortam_dosyasi = self.home / 'ortam.json'
        sahte.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                         f'open({str(ortam_dosyasi)!r}, "w").write(json.dumps(dict(os.environ)))\n'
                         'print(json.dumps({"type": "text", "sessionID": sys.argv[sys.argv.index("-m") + 1].split("/")[-1]}))\n')
        sahte.chmod(0o755)
        with patch.dict('os.environ', {'CADRE_RUN_ID': 'baska-kosu', 'CADRE_BUTCE_GOZLEM': '1'}):
            self.assertEqual(gerceklik.dusunme_metni('opencode-go/ses_d', str(sahte), db), [])
        ortam = json.loads(ortam_dosyasi.read_text())
        self.assertTrue(all(ortam.get(k) for k in ('CADRE_PYTHON', 'CADRE_HOME', 'CADRE_ENGINE_ROOT')), 'cron ortaminda kapi kurulur')
        self.assertNotIn('CADRE_RUN_ID', ortam)
        self.assertNotIn('CADRE_BUTCE_GOZLEM', ortam, 'kanarya gozlem kipinde degil, kapidan gecer')
        self.assertEqual(ortam.get('CADRE_ROL'), 'plan', 'dusunme acikca istenir; rolsuz cagrida dusunmek modele kalir')
        self.assertEqual(gerceklik.dusunme_metni('opencode-go/ses_bos', str(sahte), db),
                         ['dusunme: opencode-go/ses_bos dusunme metni uretmedi (2 deneme)'])


if __name__ == '__main__':
    unittest.main()
