"""OR-PIN: the price observation opens only approved fp8 and cached routes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import butce


class OpenRouterPinTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'zincir').mkdir()
        (self.home / 'zincir/openrouter-anahtar').write_text('fixture-not-a-key')
        self.enterContext(patch.object(butce, 'QE', self.home))
        self.enterContext(patch.object(butce, 'YURUTUCU_KIMLIK', self.home / 'yok/auth.json'))

    def test_only_allowed_cached_fp8_endpoints_set_prices(self):
        def endpoint(name, quant='fp8', cache='0.0000001', price='0.000001'):
            return {'provider_name': name, 'tag': name.lower() + ('/' + quant if quant in ('fp8', 'fp4') else ''),
                    'quantization': quant, 'pricing': {
                'prompt': price, 'completion': '0.000002', 'input_cache_read': cache}}
        rows = [endpoint('StreamLake'), endpoint('DeepInfra', price='0.0000015'),
                endpoint('Relace', price='0.01'), endpoint('DeepInfra', quant='fp4', price='0.01'),
                endpoint('StreamLake', cache=None), endpoint('StreamLake', price='Infinity')]
        requests = []
        def fetch(request, timeout):
            requests.append(request.full_url)
            return io.StringIO(json.dumps({'data': {'endpoints': rows}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=fetch):
            models = butce.or_fiyat()
        ds = models['deepseek/deepseek-v4-pro']
        self.assertEqual((ds['input'], ds['output'], ds['providers'], ds['fp8_cache']), (1.5, 2, ['streamlake/fp8', 'deepinfra/fp8'], True))
        self.assertEqual({uc: (round(k['onbellek'], 9), k['ad']) for uc, k in ds['kademeler'].items()},
                         {'streamlake/fp8': (0.1, 'StreamLake'), 'deepinfra/fp8': (0.1, 'DeepInfra')},
                         'CR-48: oturum devrinde onbellek kaybi icin uc basina okuma fiyati ve yanittaki gorunen ad')
        # 4c-7: glm by rule (fp8 + maximum price $1.4/$4.4) — every suitable endpoint without a list; for DS-flash both endpoints are above the maximum price
        self.assertEqual(models['z-ai/glm-5.3']['providers'], ['streamlake/fp8'], 'kural: uc etiketi (slug), gorunen ad degil')
        self.assertEqual(sorted(models), ['deepseek/deepseek-v4-pro', 'z-ai/glm-5.3'])
        self.assertTrue(all(url.endswith('/endpoints') for url in requests))

    def test_the_router_never_offers_an_openrouter_model_the_plugin_cannot_price(self):
        """07.10 smoke: the plan chain offered OR gpt-6.1-sol, the plugin denied it for lack of an OR-PIN price (an empty attempt on every run)."""
        import yonlendirici
        (self.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'modeller': {
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}}}))
        with patch.object(yonlendirici, 'QE', self.home), patch('butce_kapisi.available_budget', return_value=5_000_000):
            self.assertFalse(yonlendirici.butce_var('openrouter', 'openrouter/openai/gpt-6.1-sol', {'or': {}}, 1000, rol='plan')[0])
            self.assertTrue(yonlendirici.butce_var('openrouter', 'openrouter/deepseek/deepseek-v4-pro', {'or': {}}, 1000, rol='impl')[0])
            (self.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'version': 2, 'ts': time.time() - 4000, 'modeller': {
                'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}}}))
            self.assertFalse(yonlendirici.butce_var('openrouter', 'openrouter/deepseek/deepseek-v4-pro', {'or': {}}, 1000, rol='impl')[0])

    def test_closed_weight_gpt_uses_only_the_permitted_standard_tier_within_the_price_cap(self):
        """N-TS01/N-RT04: the price observation opens only the allowed standard `openai` endpoint; FLEX, Azure and fast tiers do not change the reserve."""
        def uc(tag, girdi, cikti, param):
            return {'provider_name': tag.split('/')[0], 'tag': tag, 'quantization': 'unknown', 'max_completion_tokens': 128000,
                    'supported_parameters': [param, 'reasoning', 'tools'],
                    'pricing': {'prompt': str(girdi / 1e6), 'completion': str(cikti / 1e6), 'input_cache_read': str(girdi / 1e7)}}
        rows = {'openai/gpt-6.1-sol': [uc('openai/flex', 1, 5, 'max_tokens'), uc('azure', 2, 10, 'max_completion_tokens'),
                                       uc('openai', 2, 10, 'max_tokens'), uc('azure/us', 2.2, 11, 'max_completion_tokens'),
                                       uc('amazon-bedrock/us-east-1', 2.2, 11, 'max_tokens'), uc('openai/fast', 4, 20, 'max_tokens')]}
        def fetch(request, timeout):
            ad = request.full_url.split('/models/', 1)[1].rsplit('/endpoints', 1)[0]
            return io.StringIO(json.dumps({'data': {'endpoints': rows.get(ad, [
                {'provider_name': 'StreamLake', 'quantization': 'unknown', 'pricing': {'prompt': '0.000001', 'completion': '0.000002', 'input_cache_read': '0.0000001'}}])}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=fetch):
            models = butce.or_fiyat()
        sol = models['openai/gpt-6.1-sol']
        self.assertEqual(sol['providers'], ['openai'])
        self.assertEqual((sol['input'], sol['output'], sol['kapali'], sol['max_price']), (2, 10, True, {'prompt': 2, 'completion': 10}))
        self.assertEqual(sol['kademeler']['openai']['params'], ['max_tokens'])
        self.assertNotIn('openai/flex', sol['kademeler'])
        self.assertNotIn('deepseek/deepseek-v4-pro', models, 'acik agirlikli modelde kuantizasyonu bilinmeyen uc yine kapali')

    def test_failed_refresh_keeps_the_last_good_price_for_a_while_and_retries_soon(self):
        """4c-7: a failed refresh wrote an empty file and closed OR for an hour. The last good price stays within the age limit (6 h),
        it is retried after 5 min; a price past the limit drops."""
        path = self.home / 'zincir/openrouter-fiyat.json'
        iyi = {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}
        path.write_text(json.dumps({'version': 2, 'kimlik': 'etiket', 'ts': time.time() - 4000, 'modeller': {
            'deepseek/deepseek-v4-pro': {**iyi, 'fiyat_ts': time.time() - 4000},
            'z-ai/glm-5.3': {**iyi, 'fiyat_ts': time.time() - 7 * 3600}}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertEqual(list(butce.or_fiyat()), ['deepseek/deepseek-v4-pro'])
        d = json.loads(path.read_text())
        self.assertAlmostEqual(d['sonraki'] - d['ts'], 300)
        with patch.object(butce.urllib.request, 'urlopen', side_effect=AssertionError('5 dk dolmadan yeniden denendi')):
            self.assertEqual(list(butce.or_fiyat()), ['deepseek/deepseek-v4-pro'])

    def test_prices_are_refreshed_before_the_plugin_considers_them_stale(self):
        path = self.home / 'zincir/openrouter-fiyat.json'
        iyi = {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}
        ts = time.time() - 3400
        path.write_text(json.dumps({'version': 2, 'kimlik': 'etiket', 'ts': ts, 'sonraki': ts + 3600,
                                    'modeller': {'deepseek/deepseek-v4-pro': {**iyi, 'fiyat_ts': ts}}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=OSError('tazeleme denendi')) as cagri:
            butce.or_fiyat()
        self.assertTrue(cagri.called, 'suresi dolmadan 5 dk once tazelenmeli')
        rows = [{'provider_name': 'StreamLake', 'tag': 'streamlake/fp8', 'quantization': 'fp8',
                 'pricing': {'prompt': '0.0000013', 'completion': '0.0000026', 'input_cache_read': '0.0000001'}}]
        path.write_text(json.dumps({'version': 2, 'kimlik': 'etiket', 'ts': ts, 'modeller': {}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=lambda r, timeout: io.StringIO(json.dumps({'data': {'endpoints': rows}}))):
            butce.or_fiyat()
        d = json.loads(path.read_text())
        self.assertAlmostEqual(d['sonraki'] - d['ts'], 3300, delta=1)

    def test_a_price_file_with_old_endpoint_names_is_refetched_at_once_and_never_carried(self):
        path = self.home / 'zincir/openrouter-fiyat.json'
        path.write_text(json.dumps({'version': 2, 'ts': time.time(), 'sonraki': time.time() + 3600, 'modeller': {
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['StreamLake'], 'fp8_cache': True}}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=OSError('offline')) as ag:
            self.assertEqual(butce.or_fiyat(), {})
        self.assertTrue(ag.called, 'eski bicim taze sayilmaz')
        self.assertEqual(json.loads(path.read_text())['kimlik'], 'etiket')

    def test_no_cache_price_or_quantization_means_no_route(self):
        rows = [{'provider_name': 'DeepInfra', 'pricing': {'prompt': '.1', 'completion': '.1'}}]
        with patch.object(butce.urllib.request, 'urlopen', side_effect=lambda *a, **k:
                          io.StringIO(json.dumps({'data': {'endpoints': rows}}))):
            self.assertEqual(butce.or_fiyat(), {})

    def test_unreadable_ledger_and_panel_disagreement_close_the_provider(self):
        with patch.object(butce, '_or_defter_usd', return_value=None):
            self.assertEqual(butce._or_sapma(1)['neden'], 'defter-okunamadi')
        with patch.object(butce, '_or_defter_usd', return_value=1):
            self.assertIsNone(butce._or_sapma(1), 'ilk gozlem taban olur')
            self.assertEqual(butce._or_sapma(3)['sapma'], 1, 'panel +2, defter +0 -> kapali')
            self.assertIsNotNone(butce._or_sapma(float('nan')))

    def test_divergence_is_measured_from_a_baseline_not_lifetime_totals(self):
        """06.10 review: the lifetime total comparison counted spending before the ledger (the $9.8 loss) as a permanent drift and never
        opened OR. The first observation becomes the baseline; drift is measured only on increases after the baseline."""
        defter = {'v': 0.0}
        with patch.object(butce, '_or_defter_usd', side_effect=lambda: defter['v']):
            self.assertIsNone(butce._or_sapma(9.8))
            taban = json.loads((self.home / 'zincir/openrouter-taban.json').read_text())
            self.assertEqual((taban['api_usd'], taban['defter_usd']), (9.8, 0.0))
            defter['v'] = 1.0
            self.assertIsNone(butce._or_sapma(10.8), 'panel +1.0, defter +1.0 -> uyumlu')
            defter['v'] = 1.5
            sapma = butce._or_sapma(13.0)
            self.assertIsNotNone(sapma, 'panel +3.2, defter +1.5 -> %53 sapma, $1.7 fark -> kapali')
            self.assertEqual((sapma['api_usd'], sapma['defter_usd']), (3.2, 1.5))

    def test_estimated_closures_are_a_tolerance_band_not_spend(self):
        olculen, tahmini = {'v': 0.0}, {'v': 0.0}
        with patch.object(butce, '_or_defter_usd', side_effect=lambda: olculen['v']), \
             patch.object(butce, '_or_tahmini_usd', side_effect=lambda: tahmini['v']):
            self.assertIsNone(butce._or_sapma(5.0))
            olculen['v'], tahmini['v'] = 0.02, 0.46
            self.assertIsNone(butce._or_sapma(5.02), 'hayalet cagri yapilmamis: fatura olculen kadar')
            self.assertIsNone(butce._or_sapma(5.47), 'hayalet cagri yapilmis: fatura ust sinirda')
            olculen['v'], tahmini['v'] = 0.5, 0.66
            self.assertIsNotNone(butce._or_sapma(8.0), 'fatura +3, defter araligi 0,5-1,16: sapma')

    def test_ledger_ahead_of_a_lagging_credit_api_closes_only_after_the_reporting_window(self):
        tum, eski = {'v': 0.0}, {'v': 0.0}
        with patch.object(butce, '_or_defter_usd', side_effect=lambda: tum['v']), \
             patch.object(butce, '_or_defter_eski_usd', side_effect=lambda sinir: eski['v']):
            self.assertIsNone(butce._or_sapma(22.6))
            tum['v'] = 0.58
            self.assertIsNone(butce._or_sapma(22.64), 'kredi API son harcamayi gec bildirir: defter onde, pencere icinde kapatma yok')
            eski['v'] = 0.58
            sapma = butce._or_sapma(22.64)
            self.assertIsNotNone(sapma, 'pencereden eski harcama API de hala yoksa sapma')
            self.assertEqual((sapma['api_usd'], sapma['defter_usd']), (0.04, 0.58))
            self.assertIsNotNone(butce._or_sapma(23.9), 'API defterin ustune cikarsa defter disi harcama yine kapatir')

    def test_cut_stream_reservations_close_from_the_generation_record_after_their_attempt_ended(self):
        import butce_kapisi
        import aylik_butce
        istek = butce_kapisi.BudgetLedger(self.home / 'zincir/request-budget.db')
        aylik = aylik_butce.MonthlyLedger(self.home / 'zincir/aylik-butce.db', cap=100_000_000)
        eski = time.time() - 120

        def deneme(ad, sonuc):
            yol = self.home / 'runs/kosu-1/tasks/g1/attempts' / ad
            yol.mkdir(parents=True, exist_ok=True)
            (yol / 'attempt.json').write_text(json.dumps({'attempt_id': ad, 'outcome': sonuc}))

        satirlar = {'A': ('bitti-a', 'finished', 'gen-aaa'), 'B': ('suruyor-b', 'unknown', 'gen-bbb'),
                    'C': ('bitti-c', 'finished', 'gen-ccc'), 'D': ('bitti-d', 'finished', None)}
        with istek.transaction() as con, aylik.transaction() as acon:
            for ident, (ad, sonuc, uretim) in satirlar.items():
                deneme(ad, sonuc)
                meta = {'run_id': 'kosu-1', 'task_id': 'g1', 'attempt_id': ad, 'aylik_id': 'M' + ident,
                        **({'generation': uretim} if uretim else {})}
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (ident, 'openrouter', 'p', eski, 300_000, None, None, json.dumps(meta)))
                acon.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                             ('M' + ident, 'openrouter', '2026-10', eski, 300_000, None, None, json.dumps({'session_id': 's'})))
        istenen = []

        def dokum(request, timeout):
            istenen.append(request.full_url)
            if request.full_url.endswith('gen-ccc'):
                raise OSError('dokum henuz yok')
            return io.StringIO(json.dumps({'data': {'total_cost': 0.0123}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=dokum):
            self.assertEqual(butce.or_askida_kapat(), 1)
            self.assertEqual(butce.or_askida_kapat(), 0, 'kapanan satir yeniden sorulmaz')
        self.assertEqual(sorted(u.rsplit('=', 1)[1] for u in istenen), ['gen-aaa', 'gen-ccc', 'gen-ccc'],
                         'suren denemenin ve uretim kimligi olmayan satirin dokumu sorulmaz')
        with istek.transaction() as con, aylik.transaction() as acon:
            durum = {r[0]: (r[1], r[2]) for r in con.execute('select id, actual, receipt from reservation')}
            aylik_durum = {r[0]: (r[1], r[2]) for r in acon.execute('select id, actual, receipt from reservation')}
        self.assertEqual(durum['A'], (12_300, 'A:gen-aaa'))
        self.assertEqual(aylik_durum['MA'], (12_300, 'MA:gen-aaa'), 'aylik defter ayni tutarla kapanir')
        for ident in ('B', 'C', 'D'):
            self.assertEqual(durum[ident], (None, None), ident)
            self.assertEqual(aylik_durum['M' + ident], (None, None), ident)

    def test_generation_records_and_credits_are_read_with_the_executor_key_that_made_the_calls(self):
        import butce_kapisi
        istek = butce_kapisi.BudgetLedger(self.home / 'zincir/request-budget.db')
        yol = self.home / 'runs/kosu-1/tasks/g1/attempts/bitti'
        yol.mkdir(parents=True)
        (yol / 'attempt.json').write_text(json.dumps({'attempt_id': 'bitti', 'outcome': 'finished'}))
        with istek.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('A', 'openrouter', 'p', time.time() - 120, 300_000, None, None,
                        json.dumps({'run_id': 'kosu-1', 'task_id': 'g1', 'attempt_id': 'bitti', 'generation': 'gen-aaa'})))
        kimlik = self.home / 'opencode/auth.json'
        kimlik.parent.mkdir()
        kimlik.write_text(json.dumps({'openrouter': {'type': 'api', 'key': 'fixture-executor-key'}}))
        basliklar = []

        def cevap(request, timeout):
            basliklar.append((request.full_url.split('?')[0].rsplit('/', 1)[1], request.get_header('Authorization')))
            if 'generation' in request.full_url:
                return io.StringIO(json.dumps({'data': {'total_cost': 0.01}}))
            return io.StringIO(json.dumps({'data': {'total_credits': 10, 'total_usage': 1}}))
        with patch.object(butce, 'YURUTUCU_KIMLIK', kimlik), patch.object(butce.urllib.request, 'urlopen', side_effect=cevap):
            self.assertEqual(butce.or_askida_kapat(), 1)
            butce.or_bakiye()
        self.assertEqual(basliklar, [('generation', 'Bearer fixture-executor-key'), ('credits', 'Bearer fixture-executor-key')])
        basliklar.clear()
        (self.home / 'zincir/openrouter-kredi.json').unlink(missing_ok=True)
        with patch.object(butce.urllib.request, 'urlopen', side_effect=cevap):
            butce.or_bakiye()
        self.assertEqual(basliklar, [('credits', 'Bearer fixture-not-a-key')], 'yurutucu kimligi yoksa motor anahtari kullanilir')

    def test_small_absolute_differences_do_not_close_the_provider(self):
        defter = {'v': 0.0}
        with patch.object(butce, '_or_defter_usd', side_effect=lambda: defter['v']):
            self.assertIsNone(butce._or_sapma(5.0))
            defter['v'] = 0.1
            self.assertIsNone(butce._or_sapma(5.3), 'oran %67 ama fark $0.20 < $0.50: gurultu, kapatma yok')
            defter['v'] = 0.0   # the ledger was rolled back (reset) -> the baseline is renewed, no closing
            self.assertIsNone(butce._or_sapma(4.0))
            self.assertEqual(json.loads((self.home / 'zincir/openrouter-taban.json').read_text())['api_usd'], 4.0)

    def test_nonfinite_credit_observation_never_opens_the_wallet(self):
        path = self.home / 'zincir/openrouter-kredi.json'
        path.write_text(json.dumps({'ts': time.time(), 'kalan': float('inf')}))
        self.assertIsNone(butce.or_bakiye())
        path.unlink()
        with patch.object(butce.urllib.request, 'urlopen', side_effect=lambda *a, **k:
                          io.StringIO(json.dumps({'data': {'total_credits': 'Infinity', 'total_usage': 0}}))):
            self.assertIsNone(butce.or_bakiye())

    def test_fault_clear_resets_the_openrouter_baseline_and_closure(self):
        import butce_kapisi
        for ad in ('openrouter-sapma.json', 'openrouter-taban.json'):
            (self.home / 'zincir' / ad).write_text('{}')
        (self.home / 'zincir/openrouter-kredi.json').write_text(json.dumps({'kapali': True}))
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(butce_kapisi.kurtarma(self.home, ['--ariza-temizle', 'openrouter', '--evet']), 0)
        for ad in ('openrouter-sapma.json', 'openrouter-taban.json', 'openrouter-kredi.json'):
            self.assertFalse((self.home / 'zincir' / ad).exists(), ad)

    def test_an_ended_deviation_neither_lists_nor_closes_openrouter(self):
        """4c-7: the drift file left from 04.10 listed OR as failed even though the drift had ended. A drift-free credit observation cleans
        the file; a persistent alarm (receipt ↔ breakdown) keeps OR closed until cleared manually."""
        import butce_kapisi
        sapma = self.home / 'zincir/openrouter-sapma.json'
        sapma.write_text(json.dumps({'api_usd': 9.84, 'defter_usd': 1.25, 'sapma': 0.87}))
        (self.home / 'zincir/openrouter-kredi.json').write_text(json.dumps({'ts': time.time() - 600, 'kalan': 5.0}))
        self.assertFalse(butce_kapisi.or_sapma_aktif(self.home))
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            butce_kapisi.kurtarma(self.home, ['--ariza-listele'])
        self.assertIn('pricing_fault bos', out.getvalue())
        kredi = {'data': {'total_credits': 10, 'total_usage': 4}}
        with patch.object(butce, 'ag_acik', return_value=True), patch.object(butce, '_or_sapma', return_value=None), \
             patch.object(butce.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.StringIO(json.dumps(kredi))):
            self.assertEqual(butce.or_bakiye(), 6.0)
            self.assertFalse(sapma.exists(), 'sapma bitti: eski dosya silinir')
            sapma.write_text(json.dumps({'neden': 'makbuz-dokum', 'kalici': True, 'ornekler': [{}]}))
            (self.home / 'zincir/openrouter-kredi.json').unlink()
            self.assertIsNone(butce.or_bakiye(), 'kalici alarm OR yi kapali tutar')
        self.assertTrue(butce_kapisi.or_sapma_aktif(self.home))

    def test_sampled_receipts_must_match_the_openrouter_generation_log(self):
        """4c-7(a): ledger receipts are sample-compared with OR's call breakdown; on a mismatch, a persistent drift, OR closes."""
        import sqlite3
        from contextlib import closing
        db = self.home / 'zincir/request-budget.db'
        with closing(sqlite3.connect(db)) as con:
            con.execute('create table reservation(id text primary key, provider text, period text, created real, reserved integer, '
                        'actual integer, receipt text unique, metadata text)')
            for i, (actual, gen) in enumerate(((1000, 'gen-a'), (2000, 'gen-b'), (3000, 'free-request'))):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (f'r{i}', 'openrouter', 'p', time.time() - 600 - i, 5000, actual, f'r{i}:{gen}', '{}'))
            con.commit()
        dokum = {'gen-a': 0.001, 'gen-b': 0.0021}
        def fetch(request, timeout):
            gen = request.full_url.rsplit('id=', 1)[1]
            return io.StringIO(json.dumps({'data': {'total_cost': dokum[gen]}}))
        with patch.object(butce, 'ag_acik', return_value=True), patch.object(butce.urllib.request, 'urlopen', side_effect=fetch):
            self.assertIsNone(butce.or_makbuz_denetimi(), '$0,0001 fark tolerans icinde')
            self.assertFalse((self.home / 'zincir/openrouter-sapma.json').exists())
            self.assertIsNone(butce.or_makbuz_denetimi(), '10 dk dolmadan yeniden ornekleme yok')
        kayit = self.home / 'zincir/openrouter-makbuz-denetimi.json'
        kayit.write_text(json.dumps({'ts': 0, 'denenen': []}))
        dokum['gen-b'] = 0.009
        with patch.object(butce, 'ag_acik', return_value=True), patch.object(butce.urllib.request, 'urlopen', side_effect=fetch):
            uymayan = butce.or_makbuz_denetimi()
        self.assertEqual(([u['rezervasyon'] for u in uymayan['ornekler']], uymayan['kapandi']), (['r1'], False))
        self.assertFalse((self.home / 'zincir/openrouter-sapma.json').exists(), 'tek ornek uyari, kapanis degil')
        k = json.loads(kayit.read_text())
        k.update(ts=0, denenen=[d for d in k['denenen'] if d != 'r0'])
        kayit.write_text(json.dumps(k))
        dokum['gen-a'] = 0.005
        with patch.object(butce, 'ag_acik', return_value=True), patch.object(butce.urllib.request, 'urlopen', side_effect=fetch):
            uymayan = butce.or_makbuz_denetimi()
        self.assertEqual(([u['rezervasyon'] for u in uymayan['ornekler']], uymayan['kapandi']), (['r0'], True))
        sapma = json.loads((self.home / 'zincir/openrouter-sapma.json').read_text())
        self.assertEqual((sapma['neden'], sapma['kalici']), ('makbuz-dokum', True))
        # 07.10 live: the first check sampled receipts from the 04.10 incident night (already judged) and closed OR.
        # Receipts before the baseline (first observation / failure cleanup) are not sampled.
        (self.home / 'zincir/openrouter-sapma.json').unlink()
        kayit.write_text(json.dumps({'ts': 0, 'denenen': []}))
        (self.home / 'zincir/openrouter-taban.json').write_text(json.dumps({'ts': time.time() - 300, 'api_usd': 1, 'defter_usd': 1}))
        with patch.object(butce, 'ag_acik', return_value=True), patch.object(butce.urllib.request, 'urlopen', side_effect=fetch):
            self.assertIsNone(butce.or_makbuz_denetimi())
        self.assertFalse((self.home / 'zincir/openrouter-sapma.json').exists())

    def test_openrouter_admission_uses_the_worst_case_like_openrouter_itself(self):
        """07.10 16(b) live: a Sol plan call that passed with a calibrated $0.09 reservation failed OR's own worst-case check
        (balance $0.16 < $0.78) and the reservation was left pending. On OR admission is measured with the worst case; the allocated amount stays calibrated."""
        import datetime
        import sqlite3
        from contextlib import closing
        import butce_kapisi as bk
        simdi = datetime.datetime.now()
        anlik = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(),
                 'or': {'db': 'ok', 'pay': 0.16, 'kullanilabilir': 0.16, 'kalan': 0.16}}   # remaining with margin $0.136
        defter = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        with patch.object(bk.BudgetLedger, 'kalibre', return_value=91_693):
            with self.assertRaises(bk.BudgetDenied):
                defter.reserve('openrouter', 782_292, anlik, {'model': 'openai/gpt-6.1-sol'})
            kimlik = defter.reserve('openrouter', 100_000, anlik, {'model': 'openai/gpt-6.1-sol'})
        with closing(sqlite3.connect(self.home / 'zincir/request-budget.db')) as con:
            self.assertEqual(con.execute('select reserved from reservation where id=?', (kimlik,)).fetchone()[0], 91_693)
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 1, 'reddedilen istek satir birakmaz')

    def test_the_router_skips_an_or_model_whose_endpoints_cannot_carry_the_role_output(self):
        """4c QA (Fable): the plugin removed an endpoint that could not carry the request at request time, yet the router still offered it (an empty link)."""
        import yonlendirici
        uc = lambda n: {'input': 1.3, 'output': 2.6, 'params': ['max_tokens'], 'max_cikti': n, 'kuantizasyon': 'fp8'}
        (self.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'modeller': {
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['deepinfra/fp8'], 'fp8_cache': True,
                                         'fiyat_ts': time.time(), 'kademeler': {'deepinfra/fp8': uc(16384)}}}}))
        with patch.object(yonlendirici, 'QE', self.home), patch('butce_kapisi.available_budget', return_value=5_000_000):
            self.assertFalse(yonlendirici.butce_var('openrouter', 'openrouter/deepseek/deepseek-v4-pro', {'or': {}}, 1000, rol='impl')[0])

    def test_an_endpoint_without_a_parameter_list_is_unknown_not_empty_and_old_prices_keep_their_age(self):
        """4c QA (Fable): an endpoint with no parameter list was written as [] and removed in the plugin; an old record without a stamp was refreshed as it was carried over."""
        rows = [{'provider_name': 'StreamLake', 'tag': 'streamlake/fp8', 'quantization': 'fp8', 'max_completion_tokens': 384000,
                 'pricing': {'prompt': '0.0000002', 'completion': '0.0000004', 'input_cache_read': '0.00000002'}}]
        with patch.object(butce.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.StringIO(json.dumps({'data': {'endpoints': rows}}))):
            ds = butce.or_fiyat()['deepseek/deepseek-v4-pro']
        self.assertIsNone(ds['kademeler']['streamlake/fp8']['params'])
        path = self.home / 'zincir/openrouter-fiyat.json'
        eski_ts = time.time() - 3 * 3600
        path.write_text(json.dumps({'version': 2, 'kimlik': 'etiket', 'ts': eski_ts,
                                    'modeller': {'deepseek/deepseek-v4-pro': {'input': 1, 'output': 2}}}))
        with patch.object(butce.urllib.request, 'urlopen', side_effect=OSError('offline')):
            butce.or_fiyat()
        self.assertAlmostEqual(json.loads(path.read_text())['modeller']['deepseek/deepseek-v4-pro']['fiyat_ts'], eski_ts, 3)


if __name__ == '__main__':
    unittest.main()
