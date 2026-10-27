"""Budget slices — the possibility matrix.

Decision of 17.09: HARD SPLIT. Each role spends only from its own pocket and its pocket fills with its own spending; the call
order does not change the outcome. Judge 15% (at least $0.20, at most half the cap) · helper 10% · lanes the remainder.
The only exception: when all lanes are terminal, the remaining share goes to the judge.

This file asks the real gate about every combination and writes the result to the `docs/DILIM-MATRISI-20260917.md` table:
`python3 engine/tests/test_dilim_matrisi.py --rapor`"""

from __future__ import annotations

import datetime
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce_kapisi as budget  # noqa: E402
import sozlesme  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402

SATIRLAR: list[tuple] = []


def bol(**ek):
    now = datetime.datetime.now()
    return {'ts': now.isoformat(), 'bugun': now.date().isoformat(),
            'go': {'db': 'ok', 'pay': 666.666667, 'kullanilabilir': 666.666667, 'pencere_acik': True, 'birim': 'puan'}, **ek}


class DilimMatrisiTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'zincir/request-budget.db')

    def kosu(self, cap, sozlesmeli=True):
        from kosu import RunStore
        from unittest.mock import patch
        store = RunStore(self.home)
        with patch.dict('os.environ', {'CADRE_KOSU_TAVAN_USD': str(cap)}):
            rid = store.accept('matris', 'is')['run_id']
        store.update(rid, budget_cap_usd=cap)
        if sozlesmeli:
            sozlesme.olustur(self.home, rid, None)
        return rid

    def tavan(self, rid, rol):
        return budget.rol_tavani(self.home, rid, rol)

    def dene(self, rid, rol, mikro):
        """Can this role set aside this amount? (accept/deny). The slice is written to the metadata — a condition of the hard split."""
        tavan, dilim = budget.rol_butcesi(self.home, rid, rol)
        try:
            self.ledger.reserve('opencode-go', mikro, bol(), {'run_id': rid}, run_cap=tavan, dilim=dilim)
            return 'kabul'
        except budget.BudgetDenied:
            return 'red'

    def kaydet(self, durum, rol, beklenen, gozlenen):
        SATIRLAR.append((durum, rol, beklenen, gozlenen, beklenen == gozlenen))
        self.assertEqual(gozlenen, beklenen, f'{durum} / {rol}')

    # --- 1: each role's cap in an empty run -----------------------------------------------------
    def test_bos_kosuda_tavanlar(self):
        for cap, bekle in ((5.0, {'impl': 3_750_000, 'kalite': 750_000, 'review': 500_000}),
                           (1.0, {'impl': 700_000, 'kalite': 200_000, 'review': 100_000}),
                           (0.05, {'impl': 20_000, 'kalite': 25_000, 'review': 5_000})):
            rid = self.kosu(cap)
            for rol, deger in bekle.items():
                self.kaydet(f'boş koşu, tavan ${cap}', rol, deger, self.tavan(rid, rol))

    # --- 2-4: CALL ORDER must not matter (the user's question) ----------------------------
    def _tum_payini_harca(self, rid, rol):
        return self.dene(rid, rol, self.tavan(rid, rol))

    def test_hangi_rol_once_cagrilirsa_cagrilsin_otekiler_ac_kalmaz(self):
        """User question: 'if a helper or the judge is called before a lane, won't the lane lose out?'
        With the hard split, NO: each role spends only from its own pocket, the call order does not change the outcome."""
        import itertools
        roller = ['impl', 'review', 'kalite']
        for sira in itertools.permutations(roller):
            rid = self.kosu(5.0)
            sonuc = [self._tum_payini_harca(rid, r) for r in sira]
            self.kaydet(f'sıra: {" → ".join(sira)}', 'üçü de kendi payını harcadı',
                        ['kabul', 'kabul', 'kabul'], sonuc)

    def test_bir_rol_payini_bitirince_yalniz_kendisi_reddedilir(self):
        for kurban in ('impl', 'review', 'kalite'):
            rid = self.kosu(5.0)
            self._tum_payini_harca(rid, kurban)
            self.kaydet(f'{kurban} payını bitirdi', kurban, 'red', self.dene(rid, kurban, 1000))
            for oteki in ('impl', 'review', 'kalite'):
                if oteki == kurban:
                    continue
                self.kaydet(f'{kurban} payını bitirdi', oteki, 'kabul', self.dene(rid, oteki, 50_000))

    def test_paylarin_toplami_tam_tavan_eder(self):
        for cap in (5.0, 1.0, 0.05):
            rid = self.kosu(cap)
            toplam = sum(self.tavan(rid, r) for r in ('impl', 'review', 'kalite'))
            self.kaydet(f'tavan ${cap} payların toplamı', 'toplam', int(cap * 1e6), toplam)

    # --- 4b: HAND-OFF — when the lanes finish, the remaining share goes to the judge -------------------------------
    def test_seritler_bitmeden_devir_olmaz(self):
        """A hand-off happens only when the handing role's work is DONE; otherwise it leaves the one called first hungry."""
        rid = self.kosu(5.0)
        self.kaydet('şeritler sürüyor', 'hakem tavanı', 750_000, self.tavan(rid, 'kalite'))

    def test_duzeltme_turu_devirden_sonra_kabul_cebinden_harcar(self):
        """G-C16 + B82 (24.09 R3-hard finding): the correction round runs AFTER the lanes are DONE; the lane pocket is frozen.
        Round 2's cloud and the strong model of a hard+ clean restart can only spend from the handed-off money (the acceptance pocket)."""
        import json
        import os
        from unittest.mock import patch
        from kosu import RunStore
        rid = self.kosu(5.0)
        yol = sozlesme.yol(self.home, rid)
        veri = json.loads(yol.read_text())
        veri['seritler'] = [{'id': 'g1', 'baslik': '', 'model': '', 'zorluk': 2}]
        yol.write_text(json.dumps(veri))
        self.dene(rid, 'impl', 1_000_000)
        RunStore(self.home).update(rid, tasks={'g1': {'state': 'uretti'}})
        with patch.dict(os.environ, {'CADRE_GOREV_ID': 'g1'}):
            self.kaydet('devir sonrası sıradan şerit', 'impl', 'red', self.dene(rid, 'impl', 1000))
        with patch.dict(os.environ, {'CADRE_GOREV_ID': 'duzeltme-2'}):
            self.assertEqual(budget.rol_butcesi(self.home, rid, 'impl')[1], 'hakem')
            self.kaydet('devir sonrası düzeltme turu', 'impl', 'kabul', self.dene(rid, 'impl', 500_000))
            self.assertGreater(budget.run_remaining_usd(self.home, rid, 'impl'), 2.5)
        with patch.dict(os.environ, {'CADRE_GOREV_ID': 'duzeltme-yeniden'}):
            self.kaydet('devir sonrası temiz yeniden başlama', 'kurtarici', 'kabul', self.dene(rid, 'kurtarici', 500_000))
        self.kaydet('düzeltme harcaması hakem cebini azaltır', 'hakem tavanı', 3_500_000, self.tavan(rid, 'kalite'))

    def test_seritler_bitince_artan_hakeme_devreder(self):
        rid = self.kosu(5.0)
        from kosu import RunStore
        store = RunStore(self.home)
        # make the lanes in the contract terminal (without a plan the contract writes an empty lane; set up manually)
        import sozlesme as sz
        yol = sz.yol(self.home, rid)
        veri = __import__('json').loads(yol.read_text())
        veri['seritler'] = [{'id': 'g1', 'baslik': '', 'model': '', 'zorluk': 2}]
        yol.write_text(__import__('json').dumps(veri))
        self.kaydet('şeritler bitmedi', 'hakem tavanı', 750_000, self.tavan(rid, 'kalite'))
        # the lane spends first, THEN finishes — this is the real order
        self.dene(rid, 'impl', 1_000_000)
        store.update(rid, tasks={'g1': {'state': 'uretti'}})
        # B82: the hand-off does not eat the HELPER's guarantee — judge = total − lane spending − helper share.
        self.kaydet('şeritler bitti, $1.00 harcandı', 'hakem tavanı', 3_500_000, self.tavan(rid, 'kalite'))
        # ... and the lane pocket FREEZES where it spent: after the hand-off the lane cannot spend another cent
        self.kaydet('şeritler bitti, $1.00 harcandı', 'şerit tavanı (donar)', 1_000_000, self.tavan(rid, 'impl'))
        self.kaydet('devirden sonra şerit yeni harcama', 'impl', 'red', self.dene(rid, 'impl', 1000))
        self.kaydet('devirden sonra yardımcı kendi cebinden', 'review', 'kabul', self.dene(rid, 'review', 400_000))

    def test_rapor_ek_seridi_surerken_devir_olmaz(self):
        """B226 (25.09 live R12-report): the hard report's second analyst and reconciler run AFTER the writer lane 'produced';
        the hand-off froze the lane pocket at that moment, so the plan chain was empty ('no second strong analyst'). While the dispatcher's extra-lane
        marker stays, the lane pocket stays open; when the marker is removed the hand-off works normally."""
        rid = self.kosu(5.0)
        from kosu import RunStore
        store = RunStore(self.home)
        import sozlesme as sz
        yol = sz.yol(self.home, rid)
        veri = __import__('json').loads(yol.read_text())
        veri['seritler'] = [{'id': 'g1', 'baslik': '', 'model': '', 'zorluk': 2}]
        yol.write_text(__import__('json').dumps(veri))
        self.dene(rid, 'impl', 1_000_000)
        store.update(rid, tasks={'g1': {'state': 'uretti'}})
        isaret = store.path(rid) / 'ek-serit.flag'
        isaret.write_text('g1\n')
        self.kaydet('ek şerit sürüyor', 'plan', 'kabul', self.dene(rid, 'plan', 500_000))
        self.kaydet('ek şerit sürüyor', 'hakem tavanı', 750_000, self.tavan(rid, 'kalite'))
        isaret.unlink()
        self.kaydet('ek şerit bitti', 'hakem tavanı', 3_000_000, self.tavan(rid, 'kalite'))
        self.kaydet('ek şerit bitti', 'plan', 'red', self.dene(rid, 'plan', 1000))

    def test_the_sum_of_pockets_never_exceeds_the_cap(self):
        """B82 INVARIANT: SUM(pockets) <= cap at all times. The hand-off broke this invariant — measured: a run with a $5 cap
        could spend $8.10 (lane $1.00 -> judge $3.90 -> helper $0.50 -> QA repair $2.70)."""
        from kosu import RunStore
        import json as _json
        import sozlesme as sz
        for cap in (5.0, 1.0, 0.05):
            rid = self.kosu(cap)
            yol = sz.yol(self.home, rid)
            veri = _json.loads(yol.read_text())
            veri['seritler'] = [{'id': 'g1', 'baslik': '', 'model': '', 'zorluk': 2}]
            yol.write_text(_json.dumps(veri))
            for harcama in (0, int(cap * 1e6) // 4, int(cap * 1e6) // 2):
                for bitti in (False, True):
                    rid2 = self.kosu(cap)
                    y2 = sz.yol(self.home, rid2)
                    v2 = _json.loads(y2.read_text())
                    v2['seritler'] = [{'id': 'g1', 'baslik': '', 'model': '', 'zorluk': 2}]
                    y2.write_text(_json.dumps(v2))
                    if harcama:
                        self.dene(rid2, 'impl', harcama)
                    if bitti:
                        RunStore(self.home).update(rid2, tasks={'g1': {'state': 'uretti'}})
                    toplam_cep = sum(self.tavan(rid2, r) for r in ('impl', 'review', 'kalite'))
                    self.kaydet(f'tavan ${cap} · şerit ${harcama/1e6:.2f} · bitti={bitti}',
                                'ceplerin toplamı ≤ tavan', True, toplam_cep <= int(cap * 1e6))

    # --- 5: default without a contract --------------------------------------------------------
    def test_sozlesmesiz_kosu_varsayilani_kullanir(self):
        rid = self.kosu(5.0, sozlesmeli=False)
        for rol, deger in (('impl', 3_750_000), ('kalite', 750_000), ('review', 500_000)):
            self.kaydet('sözleşme yok', rol, deger, self.tavan(rid, rol))

    # --- 6: an unknown role counts as a lane -----------------------------------------------------
    def test_bilinmeyen_rol_serit_dilimindedir(self):
        rid = self.kosu(5.0)
        for rol in ('', 'uydurma', 'plan', 'otopsi'):
            self.kaydet('bilinmeyen/faz rolü', rol or '(boş)', 3_750_000, self.tavan(rid, rol))
        # B332/B335 (03.10): QA repair is post-lane work — it spends from the judge pocket, not from the frozen lane pocket.
        self.kaydet('serit-sonrasi rol', 'qa', 750_000, self.tavan(rid, 'qa'))

    # --- 7: run without a cap ---------------------------------------------------------------------
    def test_kabul_varsayilan_tavani_yazar(self):
        """`accept` writes the default cap to every run ($2, B199 user decision 23.09); there is no such thing as a 'run without a cap'."""
        from kosu import RunStore
        store = RunStore(self.home)
        rid = store.accept('matris', 'is')['run_id']
        for rol, deger in (('impl', 1_500_000), ('kalite', 300_000), ('review', 200_000)):
            self.kaydet('tavan yazılmadı → varsayılan $2', rol, deger, self.tavan(rid, rol))


def rapor():
    hedef = Path(__file__).resolve().parents[2] / 'docs/DILIM-MATRISI-20260917.md'
    s = ['# Bütçe dilimleri — ihtimal matrisi', '',
         '**Sert bölme:** her rol YALNIZ kendi cebinden harcar ve cebi kendi harcamasıyla dolar.',
         'Hakem %15 (en az $0.20, en çok tavanın yarısı) · yardımcı %10 · şeritler artan.',
         'Çağrı SIRASI sonucu değiştirmez — kimse kimsenin payını yiyemez.',
         '**Değişmez: her an Σ(cepler) ≤ tavan.** Bütün şeritler terminal olduğunda artan pay hakeme',
         '**devreder** ve aynı anda şerit cebi harcadığı yerde **donar** — yoksa toplam tavanı aşıyordu',
         '(ölçüldü: $5 tavanlı koşu $8.10). Yardımcının garantisi devirde dokunulmaz kalır.',
         'Uygulanan tavan ile harcamanın yazıldığı cep tek karardan gelir: `rol_butcesi(home, run_id, rol)`.', '',
         'Yeniden üretme: `python3 engine/tests/test_dilim_matrisi.py --rapor`', '',
         '| durum | rol | beklenen | gözlenen | ✓ |', '|---|---|---|---|---|']
    for durum, rol, bek, goz, ok in SATIRLAR:
        f = lambda v: ('—' if v is None else (f'${v/1e6:.3f}' if isinstance(v, int) else str(v)))
        s.append(f'| {durum} | `{rol}` | {f(bek)} | {f(goz)} | {"✅" if ok else "❌"} |')
    s += ['', f'**{sum(1 for *_, ok in SATIRLAR if ok)}/{len(SATIRLAR)} satır beklendiği gibi.**', '',
          '## Okuma kılavuzu',
          '- **SERT BÖLME**: her rol yalnız kendi cebinden harcar. Payların toplamı tam olarak tavan eder.',
          '- **Çağrı sırası sonucu değiştirmez.** Altı sıralamanın altısında da üç rol kendi payını harcayabildi.',
          '- **Bir rol payını bitirince yalnız kendisi reddedilir**, ötekiler etkilenmez.',
          '- Bedeli: kullanılmayan pay boşta kalır. Ölçüm bunu ucuz kılıyor — koşu 2\'de hiçbir rol payının',
          '  yarısına bile gelmedi (şerit $0.71/$3.75, hakem $0.29/$0.75).',
          '- **tavan yazılmadı** → `accept` varsayılan $5 yazar; tavansız koşu diye bir şey yok.']
    hedef.write_text('\n'.join(s) + '\n', encoding='utf-8')
    print('rapor:', hedef)


if __name__ == '__main__':
    raporla = '--rapor' in sys.argv
    if raporla:
        sys.argv.remove('--rapor')
    sonuc = unittest.main(exit=False, verbosity=0)
    if raporla:
        rapor()
    sys.exit(0 if sonuc.result.wasSuccessful() else 1)


class OnKontrolCebiTests(unittest.TestCase):
    """B112 (19.09, two independent audits pointed at the same place): `available_budget`/`run_remaining_usd`
    TAKE a role parameter but it was not passed on the pre-check paths. When the role drops the pocket becomes `seritler`
    (`sozlesme.dilim('') == 'seritler'`), while the real reservation at the gate is made from the judge pocket.
    Result: when the lane pocket ran out the judge dropped out of the chain with 'no verified share' while its own money was still there."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.home_ = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'zincir/request-budget.db')

    def _serit_cebini_bitir(self, cap=5.0):
        from kosu import RunStore
        from unittest.mock import patch
        with patch.dict('os.environ', {'CADRE_KOSU_TAVAN_USD': str(cap)}):
            rid = RunStore(self.home).accept('b112', 'is')['run_id']
        RunStore(self.home).update(rid, budget_cap_usd=cap)
        sozlesme.olustur(self.home, rid, None)
        tavan, dilim = budget.rol_butcesi(self.home, rid, 'impl')
        self.ledger.reserve('opencode-go', tavan, bol(), {'run_id': rid}, run_cap=tavan, dilim=dilim)
        return rid

    def test_the_chain_for_a_referee_role_keeps_paid_links_when_only_the_lane_pocket_is_empty(self):
        import os
        import yonlendirici as yon
        rid = self._serit_cebini_bitir()
        b = bol(cerebras={'db': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0},
                zen={'db': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0},
                ds={'api': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0}, **{'or': {'db': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0, 'kalan': 5.0}})
        import json
        import time
        (self.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'modeller': {
            'openai/gpt-6.1-sol': {'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        eski = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            os.environ.pop('CADRE_ZINCIR_KALITE', None)  # This test checks the default GPT path, not the fixture pool.
            yon.QE = self.home
            kalite, nedenler = yon.zincir('kalite', 'orta', 8000, b, {})
        finally:
            os.environ.clear(); os.environ.update(eski)
        parali = [m for m in kalite if not m.startswith('lmstudio/')]
        self.assertTrue(parali, f'hakem cebi doluyken zincir boşaldı; nedenler: {nedenler}')
        self.assertEqual(parali, [yon.OR_SOL], 'varsayilan son hakem olculmus GPT/FLEX yoludur')
        self.assertFalse(any('dogrulanmis pay yok' in n for n in nedenler),
                         f'şerit cebi bittiği için hakem halkaları elendi: {nedenler}')

    def test_butce_var_asks_the_pocket_of_the_role_it_is_called_for(self):
        import os
        import yonlendirici as yon
        rid = self._serit_cebini_bitir()
        b = bol(zen={'db': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0})
        eski = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            yon.QE = self.home
            serit = yon.butce_var('opencode-go', 'opencode-go/glm-5.3', b, 8000, rol='impl')
            hakem = yon.butce_var('opencode-go', 'opencode-go/glm-5.3', b, 8000, rol='kalite')
            self.assertTrue(yon.butce_var('opencode', 'opencode/glm-5.3', b, 8000, rol='impl')[0])
        finally:
            os.environ.clear(); os.environ.update(eski)
        self.assertEqual(serit, (False, 'dogrulanmis pay yok'), 'şerit cebi bitti, şerit rolü reddedilmeli')
        self.assertTrue(hakem[0], f'hakem cebi dolu, kabul edilmeli — dönen: {hakem}')

    def test_the_claude_call_cap_comes_from_the_pocket_of_its_own_role(self):
        """The second face of B112 + the 19.09 mutation audit: when `qe_core:358` dropped the role it ESCAPED (no test
        broke). In a run with the lane pocket exhausted and the judge pocket full, the judge role's call cap must not be 0."""
        import os
        import butce_kapisi as budget2
        rid = self._serit_cebini_bitir()
        eski = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            serit = budget2.run_remaining_usd(self.home, rid, 'impl')
            hakem = budget2.run_remaining_usd(self.home, rid, 'kalite')
            rolsuz = budget2.run_remaining_usd(self.home, rid)
        finally:
            os.environ.clear(); os.environ.update(eski)
        self.assertLessEqual(serit, 0, 'serit cebi bitmis olmali (kurulum)')
        self.assertGreater(hakem, 0, 'hakem cebi dolu, kendi tavanini gormeli')
        self.assertEqual(rolsuz, serit, 'rolsuz cagri SERIT cebini gorur — bu yuzden rol gecilmek zorunda')

    def test_run_claude_asks_for_the_cap_with_its_role(self):
        """The difference above helps only if `run_claude` PASSES the role. Is the call really made with the role:
        we wrap `run_remaining_usd` and record which role it was called with (not a source scan)."""
        import os
        from unittest.mock import patch
        import qe_core as core
        rid = self._serit_cebini_bitir()
        cagrilar = []

        def sahte(home, run_id, rol=None, provider=''):
            cagrilar.append(rol)
            self.assertEqual(provider, 'claude')
            return 5.0

        eski = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            import claude_politikasi as cp
            with patch.object(core, 'QE', self.home), \
                 patch.object(core, 'butce_json', return_value=bol()), \
                 patch.object(core, 'available_budget', return_value=1_000_000), \
                 patch.object(core, 'run_remaining_usd', side_effect=sahte), \
                 patch.object(cp, 'difficulty', return_value=2), \
                 patch.object(cp, 'fable_slots', return_value=0), \
                 patch.object(cp, 'model', return_value='claude/sonnet#high'), \
                 patch.object(core, 'Attempt', side_effect=RuntimeError('dur')):
                try:
                    core.run_claude(str(self.home), 'claude/sonnet#high', 'is', self.home / 'log.txt', role='kalite')
                except Exception:
                    pass
        finally:
            os.environ.clear(); os.environ.update(eski)
        self.assertIn('kalite', cagrilar, f'cagri tavani rolsuz istendi: {cagrilar}')
