"""G-R1..G-R6 report/analysis mode (TASARIM-4). A report is not prose but a set of evidenced
claims: the acceptance record recognizes a report job and only the report + evidence appendix may change (G-R1), the evidence appendix is required (G-R2), report
QA verifies the claims mechanically (G-R3/G-R4), the judge probes with a sample (G-R5), the result is written by a strong model, not a local one (G-R6).
No model/network; evidence commands run in the real sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import rapor  # noqa: E402
import qe_core as core  # noqa: E402
from kosu import RunStore, calisma_kipi, execute_run  # noqa: E402
from sonuc import parse_review, revision  # noqa: E402


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)


EK1_CIKTI = "ÇIKTI: docs/reviews/2026-09-24/codex-denetim.md (yalnız bu dosyayı yaz). Türkçe, sade dil."
EK2_CIKTI = "## ÇIKTI\n`docs/reviews/2026-09-24/arac-cikti-denetimi.md` (yalnız bu dosyayı yaz). Türkçe, sade dil."
RAPOR_ISTEMI = "TUR: rapor\nRAPOR: rapor.md\napp.py'yi denetle, bulgulari raporla."
UYGULAMA = "def topla(a, b):\n    return a - b\n"   # deliberate bug: subtraction instead of addition


def git(wt, *args):
    return subprocess.run(['git', '-C', str(wt), '-c', 'user.name=fixture', '-c', 'user.email=fixture@local', *args],
                          check=True, capture_output=True, text=True, timeout=10).stdout


def depo(kok: Path) -> tuple[Path, str]:
    wt = kok / 'wt'
    wt.mkdir(parents=True)
    git(wt, 'init', '-q')
    (wt / 'app.py').write_text(UYGULAMA)
    git(wt, 'add', '.')
    git(wt, 'commit', '-qm', 'base')
    return wt, git(wt, 'rev-parse', 'HEAD').strip()


def kanit_yaz(wt: Path, rapor_metni: str, kanitlar, yol='rapor.md'):
    (wt / yol).write_text(rapor_metni)
    (wt / yol.replace('.md', '.kanit.jsonl')).write_text(''.join(json.dumps(k, ensure_ascii=False) + '\n' for k in kanitlar))


def K(i, tur, kaynak, beklenen='', iddia='iddia'):
    return {'id': f'K{i}', 'iddia': iddia, 'tur': tur, 'kaynak': kaynak, 'beklenen': beklenen}


GECERLI_RAPOR = "# Özet\n`topla` iki sayıyı toplamak yerine çıkarıyor; 2 ile 2 için 0 döner [K1][K2].\n"
GECERLI_KANIT = [K(1, 'dosya', 'app.py:2', 'return a - b'),
                 K(2, 'bug', "python3 -c 'from app import topla; assert topla(2, 2) == 4'", 'AssertionError')]


class BeyanTest(unittest.TestCase):
    def test_acik_tur_satiri_ve_rapor_yolu(self):
        b = rapor.beyan("TUR: rapor\nRAPOR: docs/denetim.md\nBOLUMLER: Özet; Huni; Hatalar\nrepoyu ölç")
        self.assertEqual(b, {'yol': 'docs/denetim.md', 'kanit': 'docs/denetim.kanit.jsonl', 'bolumler': ['Özet', 'Huni', 'Hatalar']})

    def test_yalniz_bu_dosyayi_yaz_kalibi_rapor_isidir(self):
        for istem, yol in ((EK1_CIKTI, 'docs/reviews/2026-09-24/codex-denetim.md'),
                           (EK2_CIKTI, 'docs/reviews/2026-09-24/arac-cikti-denetimi.md')):
            with self.subTest(yol=yol):
                self.assertEqual(rapor.beyan(istem)['yol'], yol)

    def test_kod_gorevi_rapor_degildir(self):
        for istem in ('product.txt dosyasini duzelt ve test ekle', 'README.md dosyasini guncelle', 'TUR: kod\nRAPOR: x.md'):
            with self.subTest(istem=istem):
                self.assertIsNone(rapor.beyan(istem))

    def test_guvensiz_rapor_yolu_kabul_edilmez(self):
        self.assertEqual(rapor.beyan("TUR: rapor\nRAPOR: ../disari.md")['yol'], 'RAPOR.md', 'acik beyanda guvensiz yol yerine varsayilan')
        self.assertIsNone(rapor.beyan("`/etc/x.md` (yalnız bu dosyayı yaz)"), 'ortuk kalipta guvensiz yol rapor kipi acmaz')

    def test_kabul_kaydi_rapor_kipini_ve_yollari_tasir(self):
        with tempfile.TemporaryDirectory() as ev:
            kayit = RunStore(Path(ev)).accept('a', "TUR: rapor\nRAPOR: r.md\nölç")
        self.assertEqual((kayit['mode'], kayit['rapor']['yol'], kayit['rapor']['kanit']), ('rapor', 'r.md', 'r.kanit.jsonl'))

    def test_salt_okur_inceleme_kipi_degismez(self):
        self.assertEqual(calisma_kipi('Bu modulu incele ve sadece rapor yaz, kod yazma'), 'inceleme')
        self.assertEqual(calisma_kipi('Bu modulu incele ve sadece rapor yaz, kod yazma\nTUR: rapor\nRAPOR: x.md'), 'rapor')

    def test_kisa_rapor_istegi_dagitima_girer(self):
        """G-R6: the pre-filter does not leave a short report request to the plain engine (the local model alone)."""
        from onelek import karar
        self.assertEqual(karar("TUR: rapor\nRAPOR: r.md\nolc", True)[0], 'GECER')
        self.assertEqual(karar("TUR: rapor\nRAPOR: r.md\nolc", False)[0], 'ATLA')


class KanitEkiTest(unittest.TestCase):
    def test_gecerli_satirlar_okunur(self):
        kayitlar, bulgular = rapor.kanit_oku('{"id":"K1","iddia":"a","tur":"dosya","kaynak":"app.py:2","beklenen":"x"}\n\n')
        self.assertEqual(([k['id'] for k in kayitlar], bulgular), (['K1'], []))

    def test_bozuk_satirlar_bulgu_uretir(self):
        metin = '\n'.join([
            'json degil',
            '{"id":"K1","iddia":"a","tur":"dosya","kaynak":"app.py:2"}',
            '{"id":"K2","iddia":"a","tur":"tahmin","kaynak":"x","beklenen":"y"}',
            '{"id":"X3","iddia":"a","tur":"url","kaynak":"https://a"}',
            '{"id":"K4","iddia":"a","tur":"url","kaynak":"https://a"}',
            '{"id":"K4","iddia":"b","tur":"url","kaynak":"https://b"}',
        ])
        _k, bulgular = rapor.kanit_oku(metin)
        self.assertEqual(len(bulgular), 5, bulgular)
        self.assertTrue(all(b.startswith('kanit-bozuk:') for b in bulgular), bulgular)

    def test_serit_onekli_kimlik_gecerli_bicim_disi_bozuk(self):
        """B224 (25.09 live R12-report): two analysts writing to the same appendix used the prefix the planner gave (K-tz1, K-yed11); the prefixed
        form is valid, an id outside the form (K-1, Ktz1, tz1) stays broken."""
        satir = '{{"id":"{}","iddia":"a","tur":"dosya","kaynak":"app.py:2","beklenen":"x"}}'
        kayitlar, bulgular = rapor.kanit_oku('\n'.join(satir.format(i) for i in ('K-tz1', 'K-yed11', 'K3', 'K-satir-1')))   # B236: prefix-dash-number is valid too
        self.assertEqual(([k['id'] for k in kayitlar], bulgular), (['K-tz1', 'K-yed11', 'K3', 'K-satir-1'], []))
        for kotu in ('K-1', 'Ktz1', 'tz1'):
            with self.subTest(kimlik=kotu):
                self.assertTrue(rapor.kanit_oku(satir.format(kotu))[1])


class IncelemeMetniTest(unittest.TestCase):
    def report(self):
        return {'Bulgular': [{'text': 'Kaynakta 4 kontrol bulunur.', 'evidence_ids': ['K1']}],
                'Belirsizlikler': [{'text': 'Kurulum sonucu dogrulanmadi.', 'evidence_ids': []}],
                'Yontem': [{'text': 'Satir 2 kaynaktan okundu.', 'evidence_ids': ['K1']}]}

    def test_citations_are_rendered_on_each_author_linked_sentence_before_its_punctuation(self):
        report = self.report()
        report['Bulgular'].append({'text': 'Aralik 10-12 kayitta mevcut', 'evidence_ids': ['K1']})
        evidence = [K(1, 'dosya', 'app.py:2', 'return a - b')]
        rendered = rapor.inceleme_metni(report, evidence)
        self.assertIn('- Kaynakta 4 kontrol bulunur [K1].', rendered)
        self.assertIn('- Aralik 10-12 kayitta mevcut [K1].', rendered)
        self.assertIn('- Satir 2 kaynaktan okundu [K1].', rendered)
        self.assertEqual(rapor.kanitsiz_sayilar(rendered), [])
        self.assertEqual(len(evidence), 1, 'reusing a source does not manufacture extra evidence')

    def test_missing_unknown_duplicate_and_inline_references_are_rejected_instead_of_invented(self):
        for refs in ([], ['K99'], ['K1', 'K1'], 'K1', [True], [{}]):
            with self.subTest(refs=refs):
                report = self.report()
                report['Bulgular'][0]['evidence_ids'] = refs
                with self.assertRaises(ValueError):
                    rapor.inceleme_metni(report, [K(1, 'dosya', 'app.py:2', 'return a - b')])
        report = self.report()
        report['Bulgular'][0]['text'] += ' [K99]'
        with self.assertRaisesRegex(ValueError, 'atiflari motor'):
            rapor.inceleme_metni(report, [K(1, 'dosya', 'app.py:2', 'return a - b')])

    def test_free_markdown_missing_sections_and_multiple_sentences_do_not_escape_the_contract(self):
        for report in ('# Bulgular\n4 kontrol.', {}, {**self.report(), 'extra': []}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                rapor.inceleme_metni(report, [])
        for text in ('', '4 kontrol. Baska 7 kontrol.', '4 kontrol.\nBaska kontrol.', '4 kontrol.\u2028# 7 kontrol',
                     '# 4 kontrol', '| 4 |', '```', '<!-- 4'):
            with self.subTest(text=text):
                report = self.report()
                report['Bulgular'][0]['text'] = text
                with self.assertRaises(ValueError):
                    rapor.inceleme_metni(report, [K(1, 'dosya', 'app.py:2', 'return a - b')])

    def test_numeric_claim_without_a_reference_in_method_remains_red(self):
        report = self.report()
        report['Yontem'][0]['evidence_ids'] = []
        rendered = rapor.inceleme_metni(report, [K(1, 'dosya', 'app.py:2', 'return a - b')])
        self.assertEqual([text for _, text in rapor.kanitsiz_sayilar(rendered)], ['- Satir 2 kaynaktan okundu.'])

    def test_turkish_section_spelling_preserves_output_and_ambiguous_duplicate_sections_are_rejected(self):
        evidence = [K(1, 'dosya', 'app.py:2', 'return a - b')]
        original = self.report()
        translated = {**original, 'Yöntem': original['Yontem']}
        translated.pop('Yontem')
        self.assertEqual(rapor.inceleme_metni(translated, evidence), rapor.inceleme_metni(original, evidence))
        with self.assertRaisesRegex(ValueError, 'ayni bolumu'):
            rapor.inceleme_metni({**original, 'Yöntem': original['Yontem']}, evidence)

    def test_abbreviation_dot_is_not_a_sentence_end_but_a_real_second_sentence_still_is(self):
        evidence = [K(1, 'dosya', 'app.py:2', 'return a - b')]
        for text in ('Ortam kurulur, örn. ROLECATCH_DISTRIBUTION yerel olur', 'Degerler sabittir, Örn. `FEEDLOOM_DB` gecicidir',
                     'Paketler sabittir, vb. Digerleri yoktur', 'Exporter checks hashes, e.g. Certifi is pinned'):
            with self.subTest(text=text):
                report = self.report()
                report['Bulgular'][0]['text'] = text
                self.assertIn(text, rapor.inceleme_metni(report, evidence))
        report = self.report()
        report['Bulgular'][0]['text'] = 'Ortam kurulur. Sonra cwd degisir'
        with self.assertRaisesRegex(ValueError, 'tek cumle'):
            rapor.inceleme_metni(report, evidence)

    def test_empty_uncertainties_are_allowed_but_findings_and_method_are_not(self):
        evidence = [K(1, 'dosya', 'app.py:2', 'return a - b')]
        rendered = rapor.inceleme_metni({**self.report(), 'Belirsizlikler': []}, evidence)
        self.assertIn('# Belirsizlikler\n\n# Yontem', rendered)
        for bolum in ('Bulgular', 'Yontem'):
            with self.subTest(bolum=bolum), self.assertRaisesRegex(ValueError, bolum + ': en az bir'):
                rapor.inceleme_metni({**self.report(), bolum: []}, evidence)

    def test_all_format_errors_are_listed_instead_of_stopping_at_the_first(self):
        report = self.report()
        report['Bulgular'] = [{'text': 'Ilk cumle. Ikinci cumle', 'evidence_ids': ['K1']},
                              {'text': 'Bilinmeyen kanit', 'evidence_ids': ['K9']},
                              {'text': 'Kanitsiz bulgu', 'evidence_ids': []}]
        report['Yontem'] = []
        with self.assertRaises(rapor.RaporBicimHatasi) as hata:
            rapor.inceleme_metni(report, [K(1, 'dosya', 'app.py:2', 'return a - b')])
        self.assertEqual([h.split(':', 1)[0] for h in hata.exception.hatalar],
                         ['Bulgular[1]', 'Bulgular[2]', 'Bulgular[3]', 'Yontem'])


class KanitsizSayiTest(unittest.TestCase):
    def test_kanitsiz_sayi_satiri_yakalanir(self):
        satirlar = rapor.kanitsiz_sayilar("# Başlık 1\n\nSon 21 günde 431 ilan elendi.\nKaynak sayısı 55 [K3].\n")
        self.assertEqual([n for n, _ in satirlar], [3])

    def test_sayi_olmayan_rakamlar_sayilmaz(self):
        metin = "\n".join([
            "## 2. Huni tablosu",
            "1. Önce ölü ilanlar temizlenir.",
            "Denetim 24.09.2026 14:30'da yapıldı; en-v30 kuralı, R1–R5 notları, G-R3 ve B203.",
            "`make check` ve `pytest -k test_2` çıktısı; eligibility.py:120 satırı; https://x.io/a1?b=2",
            "```", "toplam = 431", "```",
            "Öncelik 3: kaynak listesini gözden geçir.",
            "| Aşama | Giren | Var |", "|---|---|---|", "| kıdem | 120 | 80 [K1] |",
            "Bu oran doğrulanmadı: yaklaşık %30.",
        ])
        self.assertEqual(rapor.kanitsiz_sayilar(metin), [])

    def test_kirpilmis_cumlenin_atfi_sonraki_satirda(self):
        """B227 (25.09 live R12-report): most of the 28 unevidenced-number findings were sentences whose citation fell to the next line
        in a wrapped paragraph. The unit is the sentence: a sentence without a citation is still caught, a sentence whose citation is on the next line is evidenced."""
        metin = ("- Sarmalayıcı EACCES kazasında **rc=1** döner ve\n  bu kod tanımsızdır [K-yed8]. Eksik kaynakta 0 döner\n"
                 "  ve degraded (3) raporlanmaz [K-yed9].\n\nBirinci cümlede 5 hata var. İkincisi 7 [K1].\n")
        self.assertEqual([n for n, _ in rapor.kanitsiz_sayilar(metin)], [5])

    def test_ters_tirnakla_baslayan_cumle_ayri_denetlenir(self):
        self.assertEqual([n for n, _ in rapor.kanitsiz_sayilar("Önceki cümle 2 sonuç verdi [K1].\n`make check` 3 kez kırmızı bitti.\n")], [2])

    def test_aralik_atfi_ve_bilet_kimligi(self):
        metin = "Yedek tarafı 11 bulgunun 6'sı bug [K-yed1..K-yed11].\nİlk beş [K1–K5].\nKapsam OPS-001, BACKUP-STALE-001 ve UTF-8.\n"
        self.assertEqual(rapor.kanitsiz_sayilar(metin), [])

    def test_diyez_oneri_kimligi_sayi_degildir(self):
        self.assertEqual(rapor.kanitsiz_sayilar("- #21/#22 iddiası scriptin gerçek adımlarıyla karşılaştırılarak doğrulandı.\n"), [])
        self.assertEqual([n for n, _ in rapor.kanitsiz_sayilar("#21 iddiası 3 adımda doğrulandı.\n")], [1])

    def test_parantez_ici_kimlik_listesi_de_atiftir(self):
        """B233 (25.09 live R13-v2): the summary line '10 bugs (K4, K6), 3 risks (K5)' was counted as unevidenced; an id list in parentheses is a citation too."""
        self.assertEqual(rapor.kanitsiz_sayilar("- **Toplam bulgu:** 10 bug (K4, K6, K-yd3), 3 tasarım tercihi (K5), 3 test boşluğu (K-tz1..K-tz3).\n"), [])
        self.assertEqual(sorted(rapor.KIMLIK.findall(" ".join(rapor.REF.findall("10 bug (K4, K6) ve [K9]")))), ['K4', 'K6', 'K9'])

    def test_ciplak_kimlik_de_atiftir(self):
        """B235 (25.09 live R13-v3): the summary table '| Bug suspicion (6) | K-dn5 (trailing slash), K-dn6+K-dn27 (utm) |' was counted as unevidenced;
        an evidence id in a sentence or table row is a citation, square brackets are only one way of writing it."""
        metin = ("| Sınıf | Kanıtlar |\n|---|---|\n| Bug şüphesi (6) | K-dn5 (trailing slash), K-dn6+K-dn27 (utm) |\n\n"
                 "Toplam 3 bulgu K1 ve K2 ile desteklenir.\nBu satırda 5 sayı var ama kimlik yok.\n")
        self.assertEqual([n for n, _ in rapor.kanitsiz_sayilar(metin)], [6])
        self.assertEqual(sorted(rapor.KIMLIK.findall(" ".join(rapor.REF.findall("K-dn5 (x), K-dn6+K-dn27 ve [K9]")))), ['K-dn27', 'K-dn5', 'K-dn6', 'K9'])

    def test_onekli_kanit_atfi_sayiyi_kanitlar(self):
        self.assertEqual(rapor.kanitsiz_sayilar("Kaynak sayısı 55 [K-tz3].\nİki bulgu 7 ve 9 [K-tz1, K-yed2].\n"), [])

    def test_tablo_satirinda_kanitsiz_sayi_yakalanir(self):
        self.assertEqual([n for n, _ in rapor.kanitsiz_sayilar("| Aşama | Giren |\n|---|---|\n| dil | 57 |\n")], [3])


class KimlikAraligiTest(unittest.TestCase):
    def test_kimlik_araligi_sayi_iddiasi_degildir_gercek_sayi_yine_kirmizi(self):
        for metin in ('3. RC-OSS-005 DEVAM kalır. OSS-001\u2026004 tam kabulleri bu dilimin dışındadır.',
                      '- OSS-001..004 tam kabulleri dışındadır.', '- FIX-91...97 yerelde uygulandı.'):
            self.assertEqual(rapor.kanitsiz_sayilar(metin), [], metin)
        for metin in ('- OSS-001\u2026004 kapsamında 4 test eksik.', '- Script 12 adım çalıştırır.'):
            self.assertEqual(len(rapor.kanitsiz_sayilar(metin)), 1, metin)


class KanitSkillTest(unittest.TestCase):
    """25.09 live R12/R13: models wrote the evidence appendix wrong again and again (a finding class in the type field, multiple sources, prose 'beklenen').
    The skill file describes the same vocabulary as the code; it is bound here so the skill has to change when the code changes."""
    def test_skill_kanit_sozlugunu_kodla_ayni_anlatir(self):
        yol = Path(__file__).resolve().parents[2] / 'config/skills/rapor-kaniti/SKILL.md'
        self.assertTrue(yol.is_file(), yol)
        metin = yol.read_text()
        for tur in rapor.KANIT_TURLERI:
            self.assertIn(f'`{tur}`', metin, tur)
        for ornek in ('K1', 'K-tz1', 'K-satir-1', 'uv run', 'path:line', 'doğrulanmadı', 'cannot cite itself'):
            self.assertIn(ornek, metin, ornek)


class KomutKumuTest(unittest.TestCase):
    """B232 (25.09 live R13-v2): `uv run` could not write to `~/.cache/uv` in the sandbox, so 5/5 command evidences failed with 'rc=2'.
    The sandbox environment redirects the uv cache to a temporary directory (writable) and runs without network."""
    def test_komut_ciktisi_tavanda_kesilir_ve_diske_sinirsiz_akmaz(self):
        """D18 (01.10 Codex): the 1 MB limit only cut the returned text; the output was first written unbounded to a temporary file."""
        t = Path(tempfile.mkdtemp(prefix='kum-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt = t / 'wt'; wt.mkdir()
        rc, cikti = rapor.komut_kos('head -c 3000000 /dev/zero | tr "\\0" x; echo', wt, t / 'gecici', 60)
        self.assertEqual((rc, len(cikti)), (0, rapor.KOMUT_CIKTI_TAVANI))
        self.assertLess(sum(f.stat().st_size for f in (t / 'gecici').rglob('*') if f.is_file()), 100_000)

    def test_uv_onbellegi_gecici_dizinde_ve_yazilabilir(self):
        t = Path(tempfile.mkdtemp(prefix='kum-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt = t / 'wt'; wt.mkdir()
        rc, cikti = rapor.komut_kos('echo "$UV_CACHE_DIR|$UV_OFFLINE"; mkdir -p "$UV_CACHE_DIR" && touch "$UV_CACHE_DIR/deneme" && echo yazildi', wt, t / 'gecici', 30)
        self.assertEqual(rc, 0, cikti)
        self.assertIn(f'{(t / "gecici").resolve()}/', cikti.split('|')[0], 'uv onbellegi gecici dizinin altinda')
        self.assertIn('|1', cikti, 'UV_OFFLINE=1')
        self.assertIn('yazildi', cikti)

    def test_background_stdout_does_not_hold_up_a_finished_command(self):
        with tempfile.TemporaryDirectory(prefix='kum-boru-') as d:
            t = Path(d)
            started = time.monotonic()
            rc, output = rapor.komut_kos('sleep 6 & echo "$!"; printf "done\\n"', t, t / 'gecici', 1)
            elapsed = time.monotonic() - started
            self.assertEqual(rc, 0, output)
            self.assertIn('done', output)
            self.assertLess(elapsed, 2, 'bitmis komutun arka plan cocugu stdout kapanisini bekletmemeli')
            self.assert_process_stopped(int(output.splitlines()[0]))

    def assert_process_stopped(self, pid):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.02)
        os.kill(pid, signal.SIGKILL)
        self.fail('kanit komutunun cocugu cikista canli kaldi')

    def test_command_timeout_still_stops_the_process_group(self):
        with tempfile.TemporaryDirectory(prefix='kum-sure-') as d:
            t = Path(d)
            started = time.monotonic()
            rc, output = rapor.komut_kos('sleep 6 & echo "$!" > child.pid; wait', t, t / 'gecici', .3)
            self.assertIsNone(rc)
            self.assertIn('zaman asimi', output)
            self.assertLess(time.monotonic() - started, 2)
            self.assert_process_stopped(int((t / 'child.pid').read_text()))

    def test_cancellation_still_propagates_and_stops_the_process_group(self):
        with tempfile.TemporaryDirectory(prefix='kum-iptal-') as d:
            t = Path(d)
            started = time.monotonic()
            with patch.object(core, 'iptal_istendi', side_effect=lambda p: (t / 'child.pid').exists()):
                with self.assertRaises(core.CancelledRun):
                    rapor.komut_kos('sleep 6 & echo "$!" > child.pid; wait', t, t / 'gecici', 10,
                                   bekle=lambda p, sure: core.wait_process(p, sure, 'fixture'))
            self.assertLess(time.monotonic() - started, 2)
            self.assert_process_stopped(int((t / 'child.pid').read_text()))


class RaporQaTest(unittest.TestCase):
    """G-R3/G-R4: deterministic report QA in a real git repository and the real sandbox."""
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='rapor-qa-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.sha = depo(self.t)
        self.beyan = rapor.beyan(RAPOR_ISTEMI)

    def qa(self, beyan=None):
        return rapor.rapor_qa(str(self.wt), beyan or self.beyan, self.sha, str(self.t / 'gecici'), tohum='sabit')

    def turler(self, sonuc):
        return sorted({b.split(':', 1)[0] for b in sonuc['bulgular']})

    def test_kanitli_rapor_gecer(self):
        kanit_yaz(self.wt, GECERLI_RAPOR, GECERLI_KANIT)
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], sonuc['bulgular']), ('PASS', []))

    def test_review_file_evidence_outside_the_accepted_read_scope_is_rejected_before_it_is_read(self):
        other = self.wt / 'other.py'
        other.write_text('outside = 42\n')
        git(self.wt, 'add', 'other.py')
        git(self.wt, 'commit', '-qm', 'extra public source')
        self.sha = git(self.wt, 'rev-parse', 'HEAD').strip()
        evidence = [GECERLI_KANIT[0], K(2, 'dosya', 'other.py:1', 'outside = 42')]
        kanit_yaz(self.wt, '# Ozet\nKaynak farki var [K1][K2].\n', evidence)
        original = rapor._dosya_kanit
        with patch.object(rapor, '_dosya_kanit', wraps=original) as check:
            result = rapor.rapor_qa(self.wt, self.beyan, self.sha, self.t / 'scope-check',
                                    kaynak_yollari=[self.wt / 'app.py'])
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(self.turler(result), ['dosya-kapsam'])
        self.assertEqual([call.args[1]['id'] for call in check.call_args_list], ['K1'])
        self.assertEqual(self.qa()['status'], 'PASS', 'unrestricted report jobs keep their existing behavior')
        broad = rapor.rapor_qa(self.wt, self.beyan, self.sha, self.t / 'dir-check', kaynak_yollari=[self.wt])
        self.assertEqual(broad['status'], 'PASS', 'an explicitly selected directory includes its files')

    def test_dis_kaynak_ve_sorgu_kaniti_tarih_ister_rapor_veri_tarihi_yazar(self):
        """CR-30 / N-REP01: data of unknown freshness cannot be presented as definite fact; if a url/query is undated, the report has no data date
        line or the date is in the future, red; dated evidence + a 'Veri tarihi' line pass."""
        url = {**K(3, 'url', 'https://example.org/veri', iddia='dis kaynak')}
        sorgu = K(4, 'sorgu', "python3 -c 'print(42)'", '42')
        kanit_yaz(self.wt, GECERLI_RAPOR + "Dış kaynak 42 kayıt veriyor [K3, K4].\n", GECERLI_KANIT + [url, sorgu])
        sonuc = self.qa()
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertEqual(sorted(b.split(':', 1)[0] for b in sonuc['bulgular']), ['kaynak-tarihi', 'kaynak-tarihi', 'veri-tarihi-yok'])
        kanit_yaz(self.wt, GECERLI_RAPOR + "Dış kaynak 42 kayıt veriyor [K3, K4].\n\nVeri tarihi: 2026-10-14\n",
                  GECERLI_KANIT + [{**url, 'tarih': '2999-01-01'}, {**sorgu, 'tarih': '14.10.2026'}])
        sonuc = self.qa()
        self.assertEqual(sorted(b.split(':', 1)[0] for b in sonuc['bulgular']), ['kaynak-tarihi', 'kaynak-tarihi'])
        self.assertTrue(any('gelecekte' in b for b in sonuc['bulgular']) and any('YYYY-MM-DD' in b for b in sonuc['bulgular']))
        kanit_yaz(self.wt, GECERLI_RAPOR + "Dış kaynak 42 kayıt veriyor [K3, K4].\n\nVeri tarihi: 2026-10-14\n",
                  GECERLI_KANIT + [{**url, 'tarih': '2026-10-14'}, {**sorgu, 'tarih': '2026-10-14'}])
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], sonuc['bulgular']), ('PASS', []))

    def test_ayni_kaynaga_farkli_beklenen_celisen_kanittir(self):
        """CR-30 / N-REP01: contradictory data is explained in the report; the same source cannot silently pass with two different expected values."""
        kanit_yaz(self.wt, GECERLI_RAPOR + "Sorgu 42 ve 43 der [K5][K6].\n\nVeri tarihi: 2026-10-14\n",
                  GECERLI_KANIT + [{**K(5, 'sorgu', "python3 -c 'print(42)'", '42'), 'tarih': '2026-10-14'},
                                   {**K(6, 'sorgu', "python3 -c 'print(42)'", '43'), 'tarih': '2026-10-14'}])
        sonuc = self.qa()
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertTrue(any(b.startswith('celisen-kanit: K5, K6') for b in sonuc['bulgular']), sonuc['bulgular'])
        self.assertNotIn('celisen-kanit', ' '.join(b.split(':')[0] for b in self.qa(self.beyan)['bulgular'] if 'K1' in b))
        kanit_yaz(self.wt, GECERLI_RAPOR + "Sorgu 42 verir [K7]; ayni sorgu tek satir basar [K8].\n\nVeri tarihi: 2026-10-14\n",
                  GECERLI_KANIT + [{**K(7, 'sorgu', "python3 -c 'print(42)'", '42', iddia='deger 42'), 'tarih': '2026-10-14'},
                                   {**K(8, 'sorgu', "python3 -c 'print(42)'", 'tek satir', iddia='tek satir cikti'), 'tarih': '2026-10-14'}])
        bulgular = self.qa()['bulgular']
        self.assertFalse(any(b.startswith('celisen-kanit') for b in bulgular), ('FIX-20: ayni kaynagin farkli iddialari celiski degildir', bulgular))

    def test_rapor_ya_da_kanit_eki_yoksa_kirmizi(self):
        self.assertEqual(self.turler(self.qa()), ['kanit-yok', 'rapor-yok'])
        (self.wt / 'rapor.md').write_text(GECERLI_RAPOR)
        self.assertEqual(self.turler(self.qa()), ['bilinmeyen-kanit', 'kanit-yok'])

    def test_kanitsiz_sayi_ve_bilinmeyen_kimlik_kirmizi(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Toplam 431 ilan elendi.\nKaynak 55 [K9].\n", GECERLI_KANIT)
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['bilinmeyen-kanit', 'kanitsiz-sayi']))
        self.assertTrue(any('rapor.md:3' in b for b in sonuc['bulgular']), sonuc['bulgular'])

    def test_onekli_komut_kaniti_orneklemde_coktermez(self):
        """B229 (25.09 live R13): B224 made the prefixed id valid but the command sample sorted ids with int(id[1:]);
        'K-u1' crashed QA with a ValueError on every attempt, and the correction round could not repair it."""
        kanit_yaz(self.wt, "# Özet\n`topla` 2 ile 2 için 0 döner [K-u1].\n",
                  [{'id': 'K-u1', 'iddia': 'topla', 'tur': 'komut', 'kaynak': "python3 -c 'from app import topla; print(topla(2, 2))'", 'beklenen': '0'},
                   {'id': 'K2', 'iddia': 'kod', 'tur': 'dosya', 'kaynak': 'app.py:2', 'beklenen': 'return a - b'}])
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], sonuc['bulgular']), ('PASS', []))

    def test_rapor_ya_da_kanit_eki_kendini_kanitlayamaz(self):
        """B228 (25.09 live R12-report): the correction round based the numbers on the evidence appendix itself (K22) and on the report heading (K23);
        since the file and the quote existed QA passed it. Self-citing evidence proves no claim."""
        kanit_yaz(self.wt, GECERLI_RAPOR + "Toplam 3 kanit var [K3].\n",
                  GECERLI_KANIT + [K(3, 'dosya', 'rapor.kanit.jsonl', '"id"'), K(4, 'dosya', 'rapor.md:1', 'Özet')])
        sonuc = self.qa()
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertEqual(sorted(b.split(':', 2)[1].strip() for b in sonuc['bulgular'] if b.startswith('oz-atif')), ['K3', 'K4'])

    def test_oz_atif_mutlak_yol_ya_da_baglantiyla_atlatilamaz(self):
        """B261 (29.09 external audit): self-citation only caught the relative path; the same report's absolute path and its symlink
        passed as 'file' evidence ("999 tests passed" could be proven with its own report)."""
        mutlak = str((self.wt / 'rapor.md').resolve())
        (self.wt / 'kopya.md').symlink_to('rapor.md')
        kanit_yaz(self.wt, GECERLI_RAPOR + "999 test geçti [K3][K4].\n",
                  GECERLI_KANIT + [K(3, 'dosya', mutlak + ':3', '999 test geçti'), K(4, 'dosya', 'kopya.md:3', '999 test geçti')])
        sonuc = self.qa()
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertEqual(sorted(b.split(':', 2)[1].strip() for b in sonuc['bulgular'] if b.startswith('oz-atif')), ['K3', 'K4'])

    def test_dosya_kaniti_agac_disi_ya_da_git_izlemeyen_dosya_olamaz(self):
        """B267 (29.09 bug hunt, B261 class): a file the writer wrote itself became evidence — a file OUTSIDE the working tree,
        a file git ignores (.git/info/exclude) and a cache directory (__pycache__) did not enter the outside-the-report check."""
        dis = self.t / 'dis.txt'
        dis.write_text("999 test gecti\n")
        (self.wt / 'notlar.log').write_text("999 test gecti\n")
        (self.wt / '.git/info').mkdir(parents=True, exist_ok=True)
        (self.wt / '.git/info/exclude').write_text("notlar.log\n")
        (self.wt / '__pycache__').mkdir()
        (self.wt / '__pycache__/sonuc.txt').write_text("999 test gecti\n")
        kanit_yaz(self.wt, GECERLI_RAPOR + "999 test geçti [K3][K4][K5].\n",
                  GECERLI_KANIT + [K(3, 'dosya', str(dis) + ':1', '999 test gecti'), K(4, 'dosya', 'notlar.log:1', '999 test gecti'),
                                   K(5, 'dosya', '__pycache__/sonuc.txt:1', '999 test gecti')])
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['dosya-kaynak']), sonuc['bulgular'])
        self.assertEqual(sorted(b.split(':', 2)[1].strip() for b in sonuc['bulgular']), ['K3', 'K4', 'K5'])

    def test_bug_tavani_asilinca_denetlenmeyen_iddia_kirmizi(self):
        """B262 (29.09 external audit): bug claims after the cap were silently not run; in 11 recorded reports a fake record that
        was never run got PASS. Exceeding the cap is a finding and counts the unchecked ids."""
        self.assertEqual(rapor.BUG_TAVAN, 10)
        gercek = [K(i, 'bug', "python3 -c 'from app import topla; assert topla(2, 2) == 4'", 'AssertionError') for i in (3, 4)]
        sahte = K(5, 'bug', 'echo AssertionError', 'AssertionError')
        kanit_yaz(self.wt, GECERLI_RAPOR + "Buglar [K3][K4][K5].\n", [*GECERLI_KANIT, *gercek, sahte])
        with patch.dict(os.environ, {'CADRE_RAPOR_BUG_TAVAN': '3'}):
            sonuc = self.qa()
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['bug-tavan']), sonuc['bulgular'])
        self.assertTrue(any('K5' in b and 'K4' not in b for b in sonuc['bulgular']), sonuc['bulgular'])

    def test_onekli_bilinmeyen_kimlik_coktermez(self):
        """A leak of the B224 addition: unknown ids were sorted with int(x[1:]); 'K-tz9' would crash QA with a ValueError."""
        kanit_yaz(self.wt, GECERLI_RAPOR + "Kaynak 55 [K-tz9, K12].\n", GECERLI_KANIT)
        sonuc = self.qa()
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertTrue(any(b.startswith('bilinmeyen-kanit: K12, K-tz9') for b in sonuc['bulgular']), sonuc['bulgular'])

    def test_olmayan_dosya_satir_ve_tutmayan_alinti_kirmizi(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Ek [K3][K4][K5].\n",
                  [*GECERLI_KANIT, K(3, 'dosya', 'app.py:99', 'return'), K(4, 'dosya', 'yok.py:1', 'x'),
                   K(5, 'dosya', 'app.py:2', 'return a + b')])
        sonuc = self.qa()
        self.assertEqual(sorted(b.split(':')[1].strip() for b in sonuc['bulgular']), ['K3', 'K4', 'K5'], sonuc['bulgular'])
        self.assertEqual(self.turler(sonuc), ['dosya-kaynak'])

    def test_istekteki_kanit_siniri_mekanik_ayristirilir(self):
        self.assertEqual(rapor.kanit_siniri('En fazla 5 doğrudan dosya:satır alıntısı kullan.', 'en çok 5 dosya:satır kanıtı; kaynak değişmez'), 5)
        self.assertEqual(rapor.kanit_siniri('en fazla 3 dosya:satir kaniti', 'En cok 7 dosya:satir alintisi'), 3)
        self.assertIsNone(rapor.kanit_siniri('Kısa Bulgular ve kanıt eki ver.', ''))
        self.assertIsNone(rapor.kanit_siniri('En fazla 5 komut çalıştır.'))

    def test_kanit_siniri_besinde_bes_gecer_altisi_reddedilir_sinirsiz_istek_degismez(self):
        def yaz(adet):
            kanitlar = [K(i, 'dosya', 'app.py:2', 'return a - b') for i in range(1, adet + 1)]
            kanit_yaz(self.wt, "# Özet\n`topla` çıkarma yapar " + ''.join(f'[K{i}]' for i in range(1, adet + 1)) + ".\n", kanitlar)
        qa = lambda sinir: rapor.rapor_qa(str(self.wt), self.beyan, self.sha, str(self.t / 'gecici'), tohum='sabit', kanit_siniri=sinir)
        yaz(5)
        self.assertEqual(qa(5)['bulgular'], [], 'sinirda bes kanit gecer')
        yaz(6)
        sonuc = qa(5)
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['kanit-siniri']), sonuc['bulgular'])
        self.assertIn('en fazla 5 dosya:satir kaniti', sonuc['bulgular'][0])
        yaz(10)
        self.assertEqual(qa(None)['bulgular'], [], 'sinir belirtilmeyen istegin davranisi korunur')

    def test_kayan_satir_numarasi_kirmizi_kalir_ve_gercek_satiri_soyler(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Fark [K3].\n", [*GECERLI_KANIT, K(3, 'dosya', 'app.py:1', 'return a - b')])
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['dosya-kaynak']), sonuc['bulgular'])
        bulgu = next(b for b in sonuc['bulgular'] if b.startswith('dosya-kaynak: K3:'))
        self.assertIn('alinti dosyada satir 2', bulgu)
        kanit_yaz(self.wt, GECERLI_RAPOR + "Yok [K3].\n", [*GECERLI_KANIT, K(3, 'dosya', 'app.py:1', 'hic olmayan satir')])
        bulgu = next(b for b in self.qa()['bulgular'] if b.startswith('dosya-kaynak: K3:'))
        self.assertNotIn('dosyada satir', bulgu)

    def test_inceleme_yazari_istemi_gercek_satir_numarasi_ister_ve_arac_adi_anmaz(self):
        import kabul
        self.assertIn('nl -ba', kabul.YAZAR_KANIT_KURALI)
        self.assertIn('cat -n', kabul.YAZAR_KANIT_KURALI)
        self.assertIn('Yontem ve Belirsizlikler dahil', kabul.YAZAR_KANIT_KURALI)
        self.assertIn('rg --files', kabul.YAZAR_KANIT_KURALI)
        kaynak = Path(kabul.__file__).read_text()
        self.assertIn('+ YAZAR_KANIT_KURALI +', kaynak)
        self.assertNotIn('rapor-kaniti semasinda', kaynak)

    def test_satirsiz_dosya_kaynagi_dosyanin_tamaminda_aranir(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Tanim [K3].\n", [*GECERLI_KANIT, K(3, 'dosya', 'app.py', 'def topla(a, b):')])
        self.assertEqual(self.qa()['bulgular'], [])

    def test_hassas_yol_okunmaz(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Anahtar [K3].\n", [*GECERLI_KANIT, K(3, 'dosya', '~/.ssh/id_rsa:1', 'BEGIN')])
        sonuc = self.qa()
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertTrue(any('K3' in b and 'hassas' in b for b in sonuc['bulgular']), sonuc['bulgular'])

    def test_tutmayan_komut_kirmizi_tutan_gecer(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Bir return var [K3].\n", [*GECERLI_KANIT, K(3, 'komut', 'grep -c return app.py', '1')])
        self.assertEqual(self.qa()['bulgular'], [])
        kanit_yaz(self.wt, GECERLI_RAPOR + "Yedi return var [K3].\n", [*GECERLI_KANIT, K(3, 'komut', 'grep -c return app.py', '7')])
        self.assertEqual(self.turler(self.qa()), ['komut-kaynak'])

    def test_forbidden_executed_evidence_is_rejected_before_any_command_runs(self):
        komut = K(3, 'komut', 'touch YAN_ETKI; echo 1', '1')
        bug = K(4, 'bug', "python3 -c 'raise SystemExit(1)'", 'SystemExit')
        kanit_yaz(self.wt, GECERLI_RAPOR + "Bir return var [K3][K4].\n", [*GECERLI_KANIT, komut, bug])
        yasak = rapor.kanit_turu_yasaklari('En çok 5 dosya:satır kanıtı kullanılır, komut kanıtı verilmez')
        with patch.object(rapor, 'komut_kos', side_effect=AssertionError('yasak kanit yurutulmemeli')):
            sonuc = rapor.rapor_qa(str(self.wt), self.beyan, self.sha, str(self.t / 'gecici'), tohum='sabit', yasak_turler=yasak)
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertIn('kanit-turu-yasak', self.turler(sonuc))
        self.assertIn('K3, K4', next(b for b in sonuc['bulgular'] if b.startswith('kanit-turu-yasak')))
        self.assertFalse((self.wt / 'YAN_ETKI').exists())
        self.assertEqual(rapor.kanit_turu_yasaklari('komut kaniti yalniz gerekirse; okuma araclari serbest'), frozenset())
        self.assertEqual(rapor.kanit_turu_yasaklari('Komut kanıtı, yeni kod/test/betik, kurulum ve ağ isteği yok.'),
                         rapor.YURUTULEN_KANIT_TURLERI)

    def test_bug_iddiasi_kirmizi_test_ister(self):
        """G-R4: if the bug is real the reproduction command ends RED on the current code; if green the bug claim falls."""
        yesil = K(3, 'bug', "python3 -c 'from app import topla; assert topla(2, 2) == 0'", 'AssertionError')
        sahte = K(4, 'bug', 'echo AssertionError', 'AssertionError')   # a "reproduction" that prints the expected text but ends green
        kanit_yaz(self.wt, GECERLI_RAPOR + "Ikinci bug [K3][K4].\n", [*GECERLI_KANIT, yesil, sahte])
        sonuc = self.qa()
        self.assertEqual(self.turler(sonuc), ['bug-yeniden'])
        self.assertTrue(any('K3' in b for b in sonuc['bulgular']))
        self.assertTrue(any('K4' in b and 'rc=0' in b for b in sonuc['bulgular']), sonuc['bulgular'])

    def test_rapor_disi_dosya_degisikligi_kirmizi(self):
        """G-R1 outside-the-report; with B267 a file changed during the job (app.py, K1's source) cannot be evidence either (file-source)."""
        kanit_yaz(self.wt, GECERLI_RAPOR, GECERLI_KANIT)
        (self.wt / 'app.py').write_text(UYGULAMA + "# rapor yazarken dokunuldu\n")
        (self.wt / 'not.txt').write_text('taslak\n')
        sonuc = self.qa()
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['dosya-kaynak', 'rapor-disi']))
        self.assertTrue(any(b.startswith('dosya-kaynak: K1:') and 'degismis' in b for b in sonuc['bulgular']), sonuc['bulgular'])
        disi = next(b for b in sonuc['bulgular'] if b.startswith('rapor-disi'))
        self.assertIn('app.py', disi)
        self.assertIn('not.txt', disi)

    def test_inceleme_kipinde_bagli_kirli_kaynak_kanit_olur_sonradan_degisen_olmaz(self):
        from kosu import plan_product
        (self.wt / 'app.py').write_text(UYGULAMA + "TASINAN = 'commit edilmemis'\n")
        baslangic = plan_product(self.wt)
        beyan = {'yol': 'INCELEME.md', 'kanit': 'INCELEME.kanit.jsonl', 'bolumler': ['Bulgular']}
        metinler = ("# Bulgular\nTaşınan satır bağlı kaynakta [K1].\n",
                    json.dumps(K(1, 'dosya', 'app.py:3', "TASINAN = 'commit edilmemis'"), ensure_ascii=False))
        kos = lambda: rapor.rapor_qa(str(self.wt), beyan, self.sha, str(self.t / 'gecici'), tohum='sabit', metinler=metinler,
                                     baslangic=baslangic, kaynak_salt_okur=True)
        self.assertEqual((kos()['status'], kos()['bulgular']), ('PASS', []), 'bağlı kirli durum iş sırasında değişiklik sayılmaz')
        (self.wt / 'app.py').write_text(UYGULAMA + "TASINAN = 'commit edilmemis'\n# iş sırasında dokunuldu\n")
        sonuc = kos()
        self.assertEqual((sonuc['status'], self.turler(sonuc)), ('FAIL', ['dosya-kaynak', 'rapor-disi']), sonuc['bulgular'])
        self.assertTrue(any(b.startswith('dosya-kaynak: K1:') and 'degismis' in b for b in sonuc['bulgular']), sonuc['bulgular'])

    def test_istenen_bolum_eksikse_kirmizi(self):
        kanit_yaz(self.wt, GECERLI_RAPOR, GECERLI_KANIT)
        sonuc = self.qa({**self.beyan, 'bolumler': ['Özet', 'Hatalar']})
        self.assertEqual([b for b in sonuc['bulgular']], ['bolum-eksik: Hatalar'])

    def test_kanit_komutu_calisma_agaci_disina_yazamaz(self):
        hedef = self.t / 'disari.txt'
        kanit_yaz(self.wt, GECERLI_RAPOR + "Deneme [K3].\n", [*GECERLI_KANIT, K(3, 'komut', f'echo x > {hedef}; echo tamam', 'tamam')])
        self.qa()
        self.assertFalse(hedef.exists(), 'kanit komutu salt-okur sandbox disina yazdi')

    def test_komut_orneklemi_sinirli_ve_kararli(self):
        kanitlar = [*GECERLI_KANIT] + [K(i, 'komut', 'echo yok', f'beklenen-{i}') for i in range(3, 7)]
        kanit_yaz(self.wt, GECERLI_RAPOR + "Dort komut [K3][K4][K5][K6].\n", kanitlar)
        with patch.dict(os.environ, {'CADRE_RAPOR_KOMUT_ORNEK': '2'}):
            a, b = self.qa(), self.qa()
        self.assertEqual(len(a['bulgular']), 2, a['bulgular'])
        self.assertEqual(a['bulgular'], b['bulgular'], 'ayni urun ayni orneklemi gorur')


class YoklamaTest(unittest.TestCase):
    """G-R5: the judge picks a stratified sample from the evidence appendix and verifies it from its source; the harness applies the verdict mechanically."""
    REV = {'head': 'h', 'digest': 'd' * 64}

    def inceleme(self, yoklama, verdict='GECER'):
        return json.dumps({'revision': self.REV['digest'], 'verdict': verdict, 'yoklama': yoklama,
                           'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': 'rapor okundu, kanitlar yoklandi'}]})

    def uygula(self, raw, ids=('K1', 'K2', 'K3', 'K4', 'K5', 'K6')):
        return rapor.yoklama_uygula(raw, parse_review(raw, self.REV, ['main']), list(ids))

    def y(self, i, s='dogrulandi'):
        return {'id': i, 'sonuc': s, 'kanit': f'{i} kaynagindan okundu'}

    def test_bes_dogrulanan_yoklama_kabulu_bozmaz(self):
        self.assertEqual(self.uygula(self.inceleme([self.y(f'K{i}') for i in range(1, 6)]))['status'], 'PASS')

    def test_tek_yanlis_duzelt_uretir(self):
        r = self.uygula(self.inceleme([self.y('K1', 'yanlis')] + [self.y(f'K{i}') for i in range(2, 6)]))
        self.assertEqual((r['status'], r['verdict']), ('FAIL', 'DUZELT'))
        self.assertIn('K1', next(a['evidence'] for a in r['acceptance'] if a['id'] == 'rapor_yoklama'))

    def test_dogrulanamayan_orani_esigi_asarsa_duzelt(self):
        uc = [self.y(f'K{i}', 'dogrulanamadi') for i in range(1, 4)] + [self.y('K4'), self.y('K5')]
        iki = [self.y(f'K{i}', 'dogrulanamadi') for i in range(1, 3)] + [self.y(f'K{i}') for i in range(3, 6)]
        self.assertEqual(self.uygula(self.inceleme(uc))['status'], 'FAIL')
        self.assertEqual(self.uygula(self.inceleme(iki))['status'], 'PASS')

    def test_eksik_ya_da_uydurma_yoklama_gecersiz_incelemedir(self):
        dort = [self.y(f'K{i}') for i in range(1, 5)]
        for ad, yoklama in (('yok', []), ('az', dort), ('uydurma kimlik', [self.y('K99')] + dort),
                            ('gecersiz sonuc', [{'id': 'K5', 'sonuc': 'belki', 'kanit': 'x'}] + dort),
                            ('tekrar', [self.y('K1')] + dort)):
            with self.subTest(ad=ad):
                self.assertEqual(self.uygula(self.inceleme(yoklama))['status'], 'ERROR')

    def test_az_kanitli_raporda_esik_kanit_sayisidir(self):
        self.assertEqual(self.uygula(self.inceleme([self.y('K1'), self.y('K2')]), ids=('K1', 'K2'))['status'], 'PASS')

    def test_satir_bicimi_yoklama(self):
        raw = (f"REVIZYON: {self.REV['digest']}\nKARAR: GECER\nKRITER main: PASS - rapor okundu\n"
               + "".join(f"YOKLAMA K{i}: dogrulandi - app.py:{i} okundu\n" for i in range(1, 6)))
        self.assertEqual(self.uygula(raw)['status'], 'PASS')

    def test_satir_bicimi_onekli_kimlik(self):
        ids = ('K-tz1', 'K-tz2', 'K-yed1', 'K-yed2', 'K-yed3')
        raw = (f"REVIZYON: {self.REV['digest']}\nKARAR: GECER\nKRITER main: PASS - rapor okundu\n"
               + "".join(f"YOKLAMA {i}: dogrulandi - kaynak okundu\n" for i in ids))
        self.assertEqual(self.uygula(raw, ids=ids)['status'], 'PASS')

    def test_rapor_disi_istemde_yoklama_aranmaz(self):
        raw = self.inceleme([])
        r = parse_review(raw, self.REV, ['main'])
        self.assertEqual(rapor.yoklama_uygula(raw, r, None), r)


DENEME11_RAPOR = {
    'Bulgular': [
        {'text': 'release_smoke.py sentetik Adapter ve Transport ile collect_source çağrısı yapar', 'evidence_ids': ['K1']},
        {'text': 'release_smoke.py process_observed_items ile evaluate adımı çalıştırır', 'evidence_ids': ['K2']},
        {'text': 'release_smoke.py TestClient ile dashboard /items, /items/{id} ve /static/app.js sayfalarının renderını doğrular',
         'evidence_ids': ['K3']},
        {'text': "release_smoke.py canonical ve dashboard SQLite veritabanlarının backup'ını alır", 'evidence_ids': ['K4']},
        {'text': "Kart notundaki 'migration'dan fazlasını test eder' iddiası doğrudur; script collect, evaluate, dashboard, "
                 "static, backup/restore, reopen ve NoBrowser/DNS adımlarını da içerir", 'evidence_ids': ['K1', 'K2', 'K3', 'K4']},
        {'text': 'test_release_smoke_client.py yalnız export_smoke_client modülünden export_client fonksiyonunun lock dosyasından '
                 'paket çıkarmasını test eder; release_smoke.py smoke adımlarını assert etmez', 'evidence_ids': ['K5']},
        {'text': 'release_smoke.py sürümler arası update veya rollback mantığı içermez; backup ve restore aynı sürüm '
                 'veritabanları üzerinde çalışır', 'evidence_ids': ['K4']},
        {'text': "RC-OSS-005 kabulündeki 'mevcut lock temiz ortamda çözülür' maddesi release_smoke.py içinde doğrulanmamıştır; "
                 'bu adım yalnız test_release_smoke_client.py üzerinden export_smoke_client.py aracılığıyla test edilmektedir',
         'evidence_ids': ['K5']}],
    'Belirsizlikler': [
        {'text': 'Gerçek temiz kurulum, update ve rollback bu salt-okur dilimde doğrulanamaz; RC-OSS-005 DEVAM kalır', 'evidence_ids': []},
        {'text': 'export_smoke_client.py iç işleyişi okunmadı; lock çözümleme mantığı incelenmedi', 'evidence_ids': []}],
    'Yontem': [{'text': 'Salt-okur kod analizi; yalnız belirtilen üç dosya okundu, başka kaynak okunmadı', 'evidence_ids': []}]}
DENEME11_KANIT = [K(1, 'dosya', 'scripts/release_smoke.py:200-211', 'result = collect_source('),
                  K(2, 'dosya', 'scripts/release_smoke.py:232-239', 'outcome = process_observed_items(database, profile=profile)'),
                  K(3, 'dosya', 'scripts/release_smoke.py:254-269', 'with TestClient(create_app()) as test_client:'),
                  K(4, 'dosya', 'scripts/release_smoke.py:270-277', 'live.backup(saved)'),
                  K(5, 'dosya', 'tests/unit/test_release_smoke_client.py:11-23', 'result = export_client(ROOT / "uv.lock")')]
DENEME11_PLAN = {'gorevler': [
    {'id': 'g1', 'kabul': 'Her adim icin en az bir dosya:satir referansi iceren liste. Ortam izolasyonu, DNS/blog engeli, '
                          'collect/evaluate/dashboard/static, migration checksum, backup/restore, reopen adimlari kanitlanmis. '
                          'Script icinde update veya rollback logic olmadigi kanitlanmis.'},
    {'id': 'g2', 'kabul': 'Her testin gercek assert ettigi davranis listelenmis: lock dosyasindan paket exportu, hash varligi, '
                          'playwright yoklugu, eksik/mukerrer/tekerleksiz/kotu-hash durumlarinda ValueError. Testin release_smoke.py '
                          'icindeki smoke adimlarini assert etmedigi kanitlanmis.'}]}
DENEME11_KABUL = ('Yalnız belirtilen public script/test/kart dilimi; release_smoke adımları ve test assert kapsamına en çok 5 '
                  'dosya:satır kanıtı; kaynak değişmez; kurulum/rollback PASS, yayın kabulü veya kart kapanışı iddiası yok')
DENEME11_KART = ('#### [ ] RC-OSS-005 — Temiz install/update/rollback kalite kabulü\n'
                 '**Kabul:** fixture collect/evaluate/dashboard; aynı state ile update ve planlı rollback; migration checksum; '
                 'mevcut lock temiz ortamda çözülür.\n')


def deneme11_kaynagi(kok: Path) -> Path:
    wt = kok / 'wt'
    (wt / 'scripts').mkdir(parents=True)
    (wt / 'tests/unit').mkdir(parents=True)
    smoke = {n: f'x_{n} = {n}' for n in range(1, 306)}
    smoke.update({
        1: '"""Exercise a distribution with synthetic state and no browser or network."""',
        27: 'class NoBrowser(importlib.abc.MetaPathFinder):',
        29: '        _require(not name.startswith("playwright"), "Browser import attempted")',
        39: 'def _no_dns(*args):',
        40: '    raise OSError("DNS disabled during release smoke")',
        53: 'os.environ["FEEDLOOM_STATE_DIR"] = str(state)',
        54: 'os.chdir(workdir)',
        137: '_require(initialize(database) == ("0001",), "Fresh initialization returned wrong migrations")',
        138: '_require(initialize(database) == (), "Second initialization was not empty")',
        141: 'class Adapter:',
        174: 'class Transport:',
        200: 'result = collect_source(',
        201: '    database, adapter=Adapter(), transport=Transport(),',
        202: ')',
        232: 'outcome = process_observed_items(database, profile=profile)',
        233: '_require(not outcome.failures, "Installed evaluation failed")',
        254: 'with TestClient(create_app()) as test_client:',
        255: '    _require(test_client.get("/items").status_code == 200, "Dashboard failed")',
        256: '    _require(test_client.get(f"/items/{identifier}").status_code == 200, "Dashboard detail failed")',
        257: '    _require(test_client.get("/static/app.js").status_code == 200, "Static asset missing")',
        270: 'for name in ("canonical", "dashboard"):',
        271: '    with sqlite3.connect(state / name) as live, sqlite3.connect(backup / name) as saved:',
        272: '        live.backup(saved)',
        278: 'reopened = open_database(state)',
        279: '_require(reopened.version == version, "Same-version reopen failed")',
        283: 'restored = workdir / "restored"',
        284: 'restore_copy(backup, restored)',
    })
    (wt / 'scripts/release_smoke.py').write_text(''.join(smoke[n] + '\n' for n in range(1, 306)))
    test = {n: f'y_{n} = {n}' for n in range(1, 51)}
    test.update({
        11: 'export_client = cast(',
        13: '    runpy.run_path(str(ROOT / "scripts/export_smoke_client.py"))["export_client"],',
        17: 'def test_smoke_client_is_exported_from_lock_with_hashes() -> None:',
        18: '    result = export_client(ROOT / "uv.lock")',
        19: '    assert all(" --hash=sha256:" in line for line in result.splitlines())',
        20: '    assert "playwright" not in result',
        26: '@pytest.mark.parametrize("broken", ["missing", "duplicate", "no-wheel", "bad-hash"])',
        28: '    with pytest.raises(ValueError):',
    })
    (wt / 'tests/unit/test_release_smoke_client.py').write_text(''.join(test[n] + '\n' for n in range(1, 51)))
    git(wt, 'init', '-q')
    git(wt, 'add', '.')
    git(wt, 'commit', '-qm', 'base')
    return wt


class KanitKataloguTest(unittest.TestCase):
    """N-REP01: the writer picks the allowed range; the engine binds the source and the quote verbatim."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='kanit-katalogu-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt = self.t / 'source'
        self.wt.mkdir()

    def test_source_blocks_cover_the_whole_allowed_files_and_only_the_selected_card(self):
        script, unit, card = (self.wt / name for name in ('script.py', 'unit.py', 'BACKLOG.md'))
        script.write_text(''.join(f'variable_{n} = "source line {n}"\n' for n in range(1, 306)))
        unit.write_text(''.join(f'assert value_{n}\n' for n in range(1, 51)))
        card.write_text('OUTSIDE BEFORE\n' + 'Selected card\n' * 34 + 'OUTSIDE AFTER\n')
        catalog = rapor.inceleme_kanit_katalogu(self.wt, [script, unit, card], {card: [(2, 35)]})
        self.assertEqual(len(catalog['kanitlar']), 4)
        self.assertEqual(catalog['eksik_yollar'], [])
        self.assertNotIn('OUTSIDE', json.dumps(catalog))
        blocks = [k for k in catalog['kanitlar'] if k['kaynak'].startswith('script.py:')]
        self.assertEqual([k['kaynak'] for k in blocks], ['script.py:1-200', 'script.py:201-305'])
        self.assertEqual('\n'.join(k['metin'] for k in blocks), script.read_text().rstrip('\n'))
        for entry in catalog['kanitlar']:
            self.assertIn(entry['beklenen'], entry['metin'])
            self.assertLessEqual(len(entry['metin']), rapor.INCELEME_KANIT_METIN_TAVANI)

    def test_references_cannot_override_paths_quotes_or_use_unknown_or_duplicate_ids(self):
        source = self.wt / 'app.py'
        source.write_text('assert ready\nassert ready\n')
        catalog = rapor.inceleme_kanit_katalogu(self.wt, [source])
        reference = {'id': 'K1', 'iddia': 'ready assertion'}
        resolved = rapor.inceleme_kanit_secimi({'evidence_refs': [reference]}, catalog)
        self.assertEqual(resolved, [{'id': 'K1', 'iddia': 'ready assertion', 'tur': 'dosya',
                                     'kaynak': 'app.py:1-2', 'beklenen': 'assert ready'}])
        for bad in ([{**reference, 'kaynak': 'outside.py:1'}], [{**reference, 'beklenen': 'fabricated'}],
                    [{**reference, 'id': 'K999'}], [reference, reference], [], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rapor.inceleme_kanit_secimi({'evidence_refs': bad}, catalog)
        with self.assertRaises(ValueError):
            rapor.inceleme_kanit_secimi({'evidence_refs': [reference], 'evidence': []}, catalog)
        old = [{**resolved[0], 'kaynak': 'app.py:999'}]
        self.assertEqual(rapor.inceleme_kanit_secimi({'evidence': old}, catalog), old,
                         'gecmis elle yazilmis kanit veya yanlis satir kendiliginden duzeltilmez')

    def test_catalog_bounds_content_and_rejects_sensitive_and_escaping_paths(self):
        source = self.wt / 'large.py'
        source.write_text(('value = ' + 'x' * 990 + '\n') * 100)
        catalog = rapor.inceleme_kanit_katalogu(self.wt, [source])
        self.assertLessEqual(sum(len(k['metin']) for k in catalog['kanitlar']), rapor.INCELEME_KATALOG_METIN_TAVANI)
        self.assertEqual(catalog['eksik_yollar'], ['large.py'])
        outside = self.t / 'outside.py'
        outside.write_text('not permitted')
        sensitive = self.wt / '.env'
        sensitive.write_text('synthetic fixture only')
        alias = self.wt / 'alias.py'
        alias.symlink_to(outside)
        for path in (outside, sensitive, alias):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                rapor.inceleme_kanit_katalogu(self.wt, [path])

    def test_file_replaced_by_a_symlink_after_validation_is_not_read_into_the_catalog(self):
        source = (self.wt / 'selected.py').resolve()
        source.write_text('allowed source\n')
        outside = (self.t / 'outside.py').resolve()
        outside.write_text('OUTSIDE_SYNTHETIC_FIXTURE\n')
        real_stat, swapped = Path.stat, False

        def stat_and_swap(path, *args, **kwargs):
            nonlocal swapped
            value = real_stat(path, *args, **kwargs)
            if path == source and not swapped:
                swapped = True
                source.unlink()
                source.symlink_to(outside)
            return value

        with patch.object(Path, 'stat', stat_and_swap), self.assertRaisesRegex(ValueError, 'review-source-packet-unavailable'):
            rapor.inceleme_kanit_katalogu(self.wt, [source])
        self.assertTrue(swapped, 'gercek dosya dogrulamadan sonra degistirildi')

    def test_judge_sees_terms_after_four_thousand_characters_and_clipping_is_explicit(self):
        source = self.wt / 'script.py'
        source.write_text('# padding source xxxxxxxxxxxx\n' * 199 + 'evaluate = True\n')
        evidence = [{'id': 'K1', 'tur': 'dosya', 'kaynak': 'script.py:1-200', 'beklenen': '# padding source'}]
        items = [{'id': 'Bulgular-1', 'text': 'evaluate', 'evidence_ids': ['K1']}]
        packet = rapor.iddia_paketi(self.wt, items, evidence, [source.read_text()])[0]
        self.assertGreater(len(packet['kanit_metinleri']['K1']), 4000)
        self.assertIn('evaluate', packet['kanit_metinleri']['K1'])
        self.assertEqual(packet['kanitta_olmayan_terimler'], [])
        self.assertEqual(packet['kirpilan_kanitlar'], [])
        source.write_text(('# padding source ' + 'x' * 40 + '\n') * 199 + 'evaluate = True\n')
        packet = rapor.iddia_paketi(self.wt, items, evidence, [source.read_text()])[0]
        self.assertEqual(packet['kirpilan_kanitlar'], ['K1'])
        self.assertEqual(packet['kanitta_olmayan_terimler'], [], 'kirpilmis metin yokluk kaniti sayilmaz')


class AnlamsalKabulTest(unittest.TestCase):
    """N-REP01/CR-30: quote existence, claim support and request scope are separate decisions (the trial 11 false-PASS topology)."""
    REV = {'head': 'h', 'digest': 'd' * 64}
    KRITERLER = ['request', 'evidence', 'scope', 'task:g1', 'task:g2']

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='anlamsal-kabul-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt = deneme11_kaynagi(self.t)
        self.ogeler = rapor.inceleme_ogeleri(DENEME11_RAPOR)
        kapsam = [(self.wt / p).read_text() for p in ('scripts/release_smoke.py', 'tests/unit/test_release_smoke_client.py')]
        self.iddialar = rapor.iddia_paketi(self.wt, self.ogeler, DENEME11_KANIT, kapsam, [DENEME11_KART])
        self.gereksinimler = rapor.inceleme_gereksinimleri(DENEME11_KABUL, DENEME11_PLAN)

    def terimler(self, kimlik):
        return next(i['kanitta_olmayan_terimler'] for i in self.iddialar if i['id'] == kimlik)

    def karar(self, iddialar=None, gereksinimler=None, verdict='GECER', **ek):
        govde = {'revision': self.REV['digest'], 'verdict': verdict,
                 'acceptance': [{'id': c, 'status': 'PASS', 'evidence': 'rapor ve kaynak ayri okundu'} for c in self.KRITERLER],
                 'yoklama': [{'id': f'K{i}', 'sonuc': 'dogrulandi', 'kanit': f'K{i} alintisi kaynakta'} for i in range(1, 6)], **ek}
        if iddialar is not None:
            govde['iddialar'] = iddialar
        if gereksinimler is not None:
            govde['gereksinimler'] = gereksinimler
        raw = json.dumps(govde, ensure_ascii=False)
        review = rapor.yoklama_uygula(raw, parse_review(raw, self.REV, self.KRITERLER), [f'K{i}' for i in range(1, 6)])
        return rapor.anlamsal_karar_uygula(raw, review, self.ogeler, self.iddialar, self.gereksinimler)

    def onay(self, terim_durumu='baglam'):
        return [{'id': i['id'], 'destek': 'tam', 'gerekce': 'kanit araligi cumleyi destekler',
                 'terimler': [{'terim': t, 'durum': terim_durumu} for t in i['kanitta_olmayan_terimler']]} for i in self.iddialar]

    def kapsam(self, oge='Bulgular-1', durum='karsilandi'):
        return [{'id': g['id'], 'durum': durum, 'rapor': [oge] if durum == 'karsilandi' else [], 'gerekce': 'rapor ogesi karsilar'}
                for g in self.gereksinimler]

    def test_engine_marks_real_case_terms_that_the_cited_ranges_do_not_contain(self):
        self.assertEqual([i['id'] for i in self.iddialar], [f'Bulgular-{n}' for n in range(1, 9)])
        self.assertEqual({t.casefold() for t in self.terimler('Bulgular-5')},
                         {'migration', 'restore', 'reopen', 'nobrowser', 'dns'})
        self.assertIn('restore', [t.casefold() for t in self.terimler('Bulgular-7')])
        self.assertEqual(self.terimler('Bulgular-8'), ['RC-OSS-005'])
        for temiz in ('Bulgular-1', 'Bulgular-2', 'Bulgular-3', 'Bulgular-4', 'Bulgular-6'):
            self.assertEqual(self.terimler(temiz), [], temiz)
        k4 = next(i for i in self.iddialar if i['id'] == 'Bulgular-7')['kanit_metinleri']['K4']
        self.assertIn('live.backup(saved)', k4)
        self.assertNotIn('restore', k4, 'hakem alintiyi degil motorun okudugu gercek atif araligini gorur')

    def test_requirements_come_from_request_acceptance_and_each_plan_acceptance_clause(self):
        ids = [g['id'] for g in self.gereksinimler]
        self.assertEqual(ids[:4], ['R1', 'R2', 'R3', 'R4'])
        self.assertEqual([g['id'] for g in self.gereksinimler if g['kriter'] == 'task:g1'], ['g1.1', 'g1.2', 'g1.3'])
        izolasyon = next(g for g in self.gereksinimler if g['id'] == 'g1.2')
        self.assertIn('Ortam izolasyonu', izolasyon['metin'])
        self.assertIn('ValueError', next(g['metin'] for g in self.gereksinimler if g['id'] == 'g2.1'))

    def test_real_legacy_judge_pass_without_separate_semantic_decisions_is_not_acceptance(self):
        sonuc = self.karar()
        self.assertEqual(sonuc['status'], 'ERROR')
        self.assertIn('anlamsal karar', sonuc['reason'])

    def test_judge_cannot_say_a_flagged_term_is_inside_the_cited_range(self):
        sonuc = self.karar(self.onay('aralikta'), self.kapsam())
        self.assertEqual(sonuc['status'], 'ERROR')
        eksik = [dict(i, terimler=[]) for i in self.onay()]
        sonuc = self.karar(eksik, self.kapsam())
        self.assertEqual(sonuc['status'], 'PASS', 'terim karari zorunlu degil; karar iddia destegindedir')
        self.assertTrue(all(i['destek'] == 'tam' for i in sonuc['iddialar']), 'eksik terim karari iddiayi kendiliginden dusurmez')

    def test_honest_real_case_decisions_fail_with_claim_and_coverage_findings_for_the_repair(self):
        iddialar = self.onay()
        for karar in iddialar:
            if karar['id'] in ('Bulgular-5', 'Bulgular-7', 'Bulgular-8'):
                karar['destek'] = 'yok' if karar['id'] != 'Bulgular-7' else 'kismi'
                karar['gerekce'] = 'atif araligi bu adimlari icermiyor'
                for terim in karar['terimler']:
                    if terim['terim'].casefold() in ('restore', 'reopen', 'nobrowser', 'dns', 'migration'):
                        terim['durum'] = 'desteksiz'
        kapsam = self.kapsam()
        for g in kapsam:
            if g['id'] in ('g1.2', 'g2.1'):
                g.update(durum='eksik', rapor=[], gerekce='raporda ortam izolasyonu ve gecersiz lock assertleri yok')
        sonuc = self.karar(iddialar, kapsam)
        self.assertEqual((sonuc['status'], sonuc['verdict']), ('FAIL', 'DUZELT'))
        maddeler = {a['id']: a for a in sonuc['acceptance']}
        self.assertEqual((maddeler['iddia_destegi']['status'], maddeler['istek_kapsami']['status']), ('FAIL', 'FAIL'))
        for parca in ('Bulgular-5', 'Bulgular-7', 'Bulgular-8', 'NoBrowser'):
            self.assertIn(parca, maddeler['iddia_destegi']['evidence'])
        for parca in ('g1.2', 'g2.1', 'Ortam izolasyonu'):
            self.assertIn(parca, maddeler['istek_kapsami']['evidence'])

    def test_requirement_cannot_be_satisfied_by_an_unsupported_claim_or_by_the_judges_own_text(self):
        iddialar = self.onay()
        next(i for i in iddialar if i['id'] == 'Bulgular-5')['destek'] = 'kismi'
        sonuc = self.karar(iddialar, self.kapsam(oge='Bulgular-5'))
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertIn('Bulgular-5', next(a['evidence'] for a in sonuc['acceptance'] if a['id'] == 'istek_kapsami'))
        for ad, kapsam in (('bos rapor', [dict(g, rapor=[]) for g in self.kapsam()]),
                           ('olmayan oge', self.kapsam(oge='Bulgular-99')),
                           ('hakem aciklamasi', [dict(g, rapor=['hakem: ortam izolasyonu 27-40']) for g in self.kapsam()]),
                           ('eksik gereksinim', self.kapsam()[:-1]),
                           ('plan maddesi rapor disi', [dict(g, durum='rapor_disi', rapor=[]) for g in self.kapsam()])):
            with self.subTest(ad=ad):
                self.assertEqual(self.karar(self.onay(), kapsam)['status'], 'ERROR')

    def test_process_only_request_clause_may_be_marked_outside_the_report_with_a_reason(self):
        kapsam = self.kapsam()
        kapsam[2].update(durum='rapor_disi', rapor=[], gerekce='kaynak degismezligi motor kimligiyle denetlenir')
        self.assertEqual(kapsam[2]['id'], 'R3')
        self.assertEqual(self.karar(self.onay(), kapsam)['status'], 'PASS')

    def test_only_engine_classified_process_clauses_may_be_marked_outside_the_report(self):
        self.gereksinimler = rapor.inceleme_gereksinimleri(
            'Ortam izolasyonu ve migration adimlarini teslim edilen raporda acikla; bütçe/süre sabit; bağımsız son inceleme; '
            'kaynak değişmez; release_smoke adımları ve test assert kapsamına en çok 5 dosya:satır kanıtı; '
            'kurulum/rollback PASS, yayın kabulü veya kart kapanışı iddiası yok', {'gorevler': []})
        self.assertEqual([g['tur'] for g in self.gereksinimler], ['icerik', 'surec', 'surec', 'surec', 'icerik', 'icerik'])
        for kimlik in ('R1', 'R5', 'R6'):
            with self.subTest(icerik=kimlik):
                kapsam = self.kapsam()
                next(g for g in kapsam if g['id'] == kimlik).update(durum='rapor_disi', rapor=[], gerekce='surec kurali sayildi')
                sonuc = self.karar(self.onay(), kapsam)
                self.assertEqual(sonuc['status'], 'ERROR')
                self.assertIn(kimlik, sonuc['reason'])
        kapsam = self.kapsam()
        for g in kapsam:
            if g['id'] in ('R2', 'R3', 'R4'):
                g.update(durum='rapor_disi', rapor=[], gerekce='motor butce, kaynak kimligi ve bagimsiz oturumu denetler')
        self.assertEqual(self.karar(self.onay(), kapsam)['status'], 'PASS')
        ek = self.kapsam() + [{'id': 'ek-1', 'metin': 'Bütçe sabit kalır', 'durum': 'rapor_disi', 'rapor': [], 'gerekce': 'hakem ekledi'}]
        self.assertEqual(self.karar(self.onay(), ek)['status'], 'ERROR', 'hakemin ekledigi madde rapor disi istisna acamaz')

    def test_plan_task_clauses_use_the_same_process_classifier(self):
        gereksinimler = rapor.inceleme_gereksinimleri('kaynak değişmez; PASS iddiası yok', {'gorevler': [
            {'id': 'g1', 'kabul': 'kaynak degismeyecek; PASS iddiasi yazilmaz; bagimsiz son inceleme yapilacak'}]})
        self.assertEqual([(g['id'], g['tur']) for g in gereksinimler],
                         [('R1', 'surec'), ('R2', 'icerik'), ('g1.1', 'surec'), ('g1.2', 'icerik'), ('g1.3', 'surec')])

    def test_judge_rule_requires_report_items_for_a_met_requirement(self):
        self.assertIn('karsilandi icin rapor listesi bos olamaz', rapor.ANLAMSAL_HAKEM_KURALI)
        self.assertIn('yoklugu acikca soyleyen rapor ogesiyle', rapor.ANLAMSAL_HAKEM_KURALI)

    def test_negative_decision_overrides_a_gecer_verdict_and_consistent_decisions_pass(self):
        iddialar = self.onay()
        iddialar[0]['destek'] = 'kismi'
        self.assertEqual(self.karar(iddialar, self.kapsam(oge='Belirsizlikler-1'))['status'], 'FAIL')
        sonuc = self.karar(self.onay(), self.kapsam())
        self.assertEqual(sonuc['status'], 'PASS')
        self.assertEqual({a['id']: a['status'] for a in sonuc['acceptance'] if a['id'] in ('iddia_destegi', 'istek_kapsami')},
                         {'iddia_destegi': 'PASS', 'istek_kapsami': 'PASS'})


DENEME22_KABUL = (
    'Yalnız belirtilen public script, birim testi ve seçili RC-OSS-005 kart dilimi salt-okur incelenir; Kaynak değişmez; '
    'Geçici state ve ayar dosyalarıyla ortam hazırlığının kodda nasıl kurulduğu raporda somut açıklanır; '
    'Çalışma dizininin değiştirilmesi ve geri alınması raporda somut açıklanır; '
    'Testin olumlu çıktı assert koşulları beklenen değerleriyle Bulgular bölümünde somut yazılır, yalnız assertler incelendi '
    'diyen yöntem cümlesi karşılama sayılmaz; Testin olumsuz fixture durumları ve beklenen hata Bulgular bölümünde somut yazılır; '
    'Bağımsız son inceleme; Bütçe/süre sabit')
DENEME22_RAPOR = {
    'Bulgular': [
        {'text': 'Script TemporaryDirectory ile geçici dizin oluşturup FEEDLOOM_DB, ROLECATCH_DISTRIBUTION gibi env '
                 'değişkenlerini bu dizine bağlar; ROLECATCH_SOURCE_POLICY kalıntısını temizler.', 'evidence_ids': ['K2']},
        {'text': 'Testin olumsuz fixture durumları: missing, duplicate, no-wheel, bad-hash bozuk lock içeriklerinde '
                 'export_client ValueError fırlatır.', 'evidence_ids': ['K4']},
        {'text': 'Testin olumsuz fixture durumları missing, duplicate, no-wheel ve bad-hash problemleriyle parametrize '
                 'edilip her durumda ValueError beklenmesidir', 'evidence_ids': ['K4']},
        {'text': 'Script FEEDLOOM_DB ve ROLECATCH_REQUEST_STATE yollarını geçici dizine bağlar, ROLECATCH_DISTRIBUTION '
                 'değişkenine ise sabit "local" değerini verir.', 'evidence_ids': ['K2']}],
    'Belirsizlikler': [],
    'Yontem': [{'text': 'Yalnız izinli iki dosya salt-okur incelendi.', 'evidence_ids': []}]}
DENEME22_KANIT = [
    K(2, 'dosya', 'scripts/release_smoke.py:1-200', '"""Exercise a distribution with disposable synthetic state and no browser/network.'),
    K(3, 'dosya', 'scripts/release_smoke.py:201-305', 'y = 201'),
    K(4, 'dosya', 'tests/unit/test_release_smoke_client.py:1-50', 'from __future__ import annotations')]
DENEME22_SMOKE = {
    1: '"""Exercise a distribution with disposable synthetic state and no browser/network.',
    51: '    with TemporaryDirectory(prefix="rolecatch-release-smoke-") as directory:',
    52: '        state = Path(directory)',
    53: '        os.environ.update(',
    54: '            FEEDLOOM_DB=str(state / "canonical.sqlite3"),',
    55: '            FEEDLOOM_DASHBOARD_DB=str(state / "dashboard.sqlite3"),',
    56: '            FEEDLOOM_CV_PROFILE=str(state / "missing-cv.json"),',
    57: '            FEEDLOOM_SETTINGS=str(state / "settings.json"),',
    58: '            FEEDLOOM_WARM_ON_START="0",',
    59: '            FEEDLOOM_ALLOW_REGISTRATION="0",',
    60: '            ROLECATCH_DISTRIBUTION="local",',
    61: '            ROLECATCH_REQUEST_STATE=str(state / "request.sqlite3"),',
    62: '        )',
    63: '        os.environ.pop("ROLECATCH_SOURCE_POLICY", None)',
    147: '            return PageRequest("https://fixture.example.org/api/items")',
    186: '        kind="fixture",',
    201: 'y = 201'}
DENEME22_TEST = {
    1: 'from __future__ import annotations',
    26: '@pytest.mark.parametrize("problem", ["missing", "duplicate", "no-wheel", "bad-hash"])',
    27: 'def test_smoke_client_refuses_incomplete_or_ambiguous_lock(',
    28: '    tmp_path: Path, problem: str',
    47: '    lock = tmp_path / "uv.lock"',
    49: '    with pytest.raises(ValueError):',
    50: '        export_client(lock)'}


class Deneme20_22Test(unittest.TestCase):
    REV = {'head': 'h', 'digest': 'e' * 64}
    KRITERLER = ['request', 'evidence', 'scope']

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='deneme22-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt = self.t / 'wt'
        (self.wt / 'scripts').mkdir(parents=True)
        (self.wt / 'tests/unit').mkdir(parents=True)
        (self.wt / 'scripts/release_smoke.py').write_text(
            ''.join(DENEME22_SMOKE.get(n, f'x_{n} = {n}') + '\n' for n in range(1, 306)))
        (self.wt / 'tests/unit/test_release_smoke_client.py').write_text(
            ''.join(DENEME22_TEST.get(n, f'y_{n} = {n}') + '\n' for n in range(1, 51)))
        adlar = ['scripts/release_smoke.py', 'tests/unit/test_release_smoke_client.py']
        self.ogeler = rapor.inceleme_ogeleri(DENEME22_RAPOR)
        self.iddialar = rapor.iddia_paketi(self.wt, self.ogeler, DENEME22_KANIT, [(self.wt / a).read_text() for a in adlar],
                                           kapsam_adlari=adlar, istek=DENEME22_KABUL)
        self.gereksinimler = rapor.inceleme_gereksinimleri(DENEME22_KABUL, {'gorevler': []})

    def paket(self, kimlik):
        return next(i for i in self.iddialar if i['id'] == kimlik)

    def gereksinim(self, parca):
        return next(g['id'] for g in self.gereksinimler if parca in g['metin'])

    def karar(self, iddialar, gereksinimler, verdict='GECER'):
        raw = json.dumps({'revision': self.REV['digest'], 'verdict': verdict,
                          'acceptance': [{'id': c, 'status': 'PASS', 'evidence': 'rapor ve kaynak ayri okundu'} for c in self.KRITERLER],
                          'yoklama': [{'id': k['id'], 'sonuc': 'dogrulandi', 'kanit': 'alinti kaynakta'} for k in DENEME22_KANIT],
                          'iddialar': iddialar, 'gereksinimler': gereksinimler}, ensure_ascii=False)
        review = rapor.yoklama_uygula(raw, parse_review(raw, self.REV, self.KRITERLER), [k['id'] for k in DENEME22_KANIT], en_az=3)
        return raw, rapor.anlamsal_karar_uygula(raw, review, self.ogeler, self.iddialar, self.gereksinimler)

    def onay(self, durum='esdeger', **degisen):
        return [{'id': i['id'], 'destek': degisen.get(i['id'], 'tam'), 'gerekce': 'atif araligi cumlenin davranisini gosterir',
                 'terimler': [{'terim': t, 'durum': durum} for t in i['kanitta_olmayan_terimler']]} for i in self.iddialar]

    def kapsam(self, oge='Bulgular-4'):
        return [{'id': g['id'], 'durum': 'karsilandi' if g['tur'] == 'icerik' else 'rapor_disi',
                 'rapor': [oge] if g['tur'] == 'icerik' else [], 'gerekce': 'rapor ogesi karsilar'} for g in self.gereksinimler]

    def test_flagged_request_word_shows_where_the_scope_uses_it_and_that_it_comes_from_the_request(self):
        for kimlik in ('Bulgular-2', 'Bulgular-3'):
            paket = self.paket(kimlik)
            self.assertEqual(paket['kanitta_olmayan_terimler'], ['fixture'], kimlik)
            uyari = paket['terim_uyarilari'][0]
            self.assertEqual((uyari['terim'], uyari['istek_terimi']), ('fixture', True))
            self.assertEqual(uyari['kapsam_satirlari'],
                             ['scripts/release_smoke.py:147: return PageRequest("https://fixture.example.org/api/items")',
                              'scripts/release_smoke.py:186: kind="fixture",'])
        self.assertIn('tek basina destek dusurmez', rapor.ANLAMSAL_HAKEM_KURALI)
        self.assertIn('bulunmasi da', rapor.ANLAMSAL_HAKEM_KURALI)

    def test_word_warning_alone_does_not_reject_but_a_real_unsupported_claim_does(self):
        raw, sonuc = self.karar(self.onay('esdeger'), self.kapsam())
        self.assertEqual(sonuc['status'], 'PASS', sonuc.get('reason') or sonuc.get('acceptance'))
        self.assertEqual(self.karar(self.onay('baglam'), self.kapsam())[1]['status'], 'PASS')
        _, sonuc = self.karar(self.onay('desteksiz'), self.kapsam())
        self.assertEqual((sonuc['status'], sonuc['verdict']), ('FAIL', 'DUZELT'))
        self.assertIn('fixture', next(a['evidence'] for a in sonuc['acceptance'] if a['id'] == 'iddia_destegi'))

    def test_identifier_present_in_the_cited_range_is_not_proof_of_the_claimed_value(self):
        paket = self.paket('Bulgular-1')
        self.assertEqual(paket['kanitta_olmayan_terimler'], [], 'terimler aralikta gecer; sozcuk uyarisi yok')
        self.assertEqual(paket['kod_satirlari']['ROLECATCH_DISTRIBUTION'], ['K2:60: ROLECATCH_DISTRIBUTION="local",'])
        self.assertEqual(paket['kod_satirlari']['FEEDLOOM_DB'], ['K2:54: FEEDLOOM_DB=str(state / "canonical.sqlite3"),'])
        self.assertEqual(paket['kod_satirlari']['ROLECATCH_SOURCE_POLICY'], ['K2:63: os.environ.pop("ROLECATCH_SOURCE_POLICY", None)'])
        self.assertEqual(self.paket('Bulgular-4')['kod_satirlari']['ROLECATCH_DISTRIBUTION'],
                         ['K2:60: ROLECATCH_DISTRIBUTION="local",'])
        ortam = self.gereksinim('Geçici state')
        kapsam = self.kapsam()
        next(g for g in kapsam if g['id'] == ortam)['rapor'] = ['Bulgular-1']
        _, sonuc = self.karar(self.onay(**{'Bulgular-1': 'kismi'}), kapsam)
        self.assertEqual(sonuc['status'], 'FAIL')
        maddeler = {a['id']: a['evidence'] for a in sonuc['acceptance'] if a['status'] == 'FAIL'}
        self.assertIn('Bulgular-1 destek=kismi', maddeler['iddia_destegi'])
        self.assertIn(f'{ortam} desteklenmeyen ogeye dayaniyor: Bulgular-1', maddeler['istek_kapsami'])
        _, dogru = self.karar(self.onay(**{'Bulgular-1': 'kismi'}), self.kapsam('Bulgular-4'))
        self.assertEqual(dogru['status'], 'FAIL', 'yanlis cumle raporda kaldikca dogru cumle onu temizlemez')
        rapor_ = {**DENEME22_RAPOR, 'Bulgular': DENEME22_RAPOR['Bulgular'][1:]}
        self.ogeler = rapor.inceleme_ogeleri(rapor_)
        adlar = ['scripts/release_smoke.py', 'tests/unit/test_release_smoke_client.py']
        self.iddialar = rapor.iddia_paketi(self.wt, self.ogeler, DENEME22_KANIT, [(self.wt / a).read_text() for a in adlar],
                                           kapsam_adlari=adlar, istek=DENEME22_KABUL)
        self.assertEqual(self.karar(self.onay(), self.kapsam('Bulgular-3'))[1]['status'], 'PASS', 'dogru anlatim gecer')

    def test_extra_decisions_for_unflagged_code_terms_are_not_a_protocol_error_but_unsupported_ones_still_fail(self):
        iddialar = self.onay()
        iddialar[0]['terimler'] = [{'terim': 'ROLECATCH_DISTRIBUTION', 'durum': 'olumsuz'}, {'terim': 'FEEDLOOM_DB', 'durum': 'esdeger'}]
        _, sonuc = self.karar(iddialar, self.kapsam())
        self.assertEqual(sonuc['status'], 'PASS', sonuc.get('reason'))
        self.assertEqual(next(i for i in sonuc['iddialar'] if i['id'] == 'Bulgular-1')['terimler'], [])
        iddialar[0]['terimler'][0]['durum'] = 'desteksiz'
        _, sonuc = self.karar(iddialar, self.kapsam())
        self.assertEqual(sonuc['status'], 'FAIL')
        self.assertIn('ROLECATCH_DISTRIBUTION', next(a['evidence'] for a in sonuc['acceptance'] if a['id'] == 'iddia_destegi'))
        tekrar = self.onay()
        tekrar[1]['terimler'] = tekrar[1]['terimler'] * 2
        self.assertEqual(self.karar(tekrar, self.kapsam())[1]['status'], 'ERROR', 'ayni terime iki karar gecmez')

    def test_incomplete_judge_answer_never_passes_but_bound_concrete_findings_are_kept_for_one_repair(self):
        assertler = self.gereksinim('olumlu çıktı assert')
        iddialar = [i for i in self.onay() if i['id'] != 'Bulgular-2']
        kapsam = self.kapsam()
        next(g for g in kapsam if g['id'] == assertler).update(durum='eksik', rapor=[],
                                                               gerekce='olumlu assertlerin beklenen degerleri raporda yok')
        iddialar[0].update(destek='kismi', gerekce='ROLECATCH_DISTRIBUTION gecici dizine degil "local" degerine atanir')
        raw, sonuc = self.karar(iddialar, kapsam, verdict='DUZELT')
        self.assertEqual(sonuc['status'], 'ERROR', 'iddia karari eksik cevap kabul ya da ret sayilmaz')
        bulgular = rapor.somut_duzeltmeler(raw, self.REV['digest'], self.iddialar, self.gereksinimler, self.KRITERLER)
        self.assertEqual([(b['tur'], b['id']) for b in bulgular], [('iddia', 'Bulgular-1'), ('gereksinim', assertler)])
        self.assertIn('"local"', bulgular[0]['gerekce'])
        govde = json.loads(raw)
        for ad, bozuk in (('baska revizyon', {**govde, 'revision': 'f' * 64}),
                          ('gecer hukmu', {**govde, 'verdict': 'GECER'}),
                          ('bilinmeyen kimlik', {**govde, 'iddialar': [dict(iddialar[0], id='Bulgular-99')], 'gereksinimler': []}),
                          ('yer tutucu gerekce', {**govde, 'iddialar': [dict(iddialar[0], gerekce='...')], 'gereksinimler': []})):
            with self.subTest(ad=ad):
                self.assertEqual(rapor.somut_duzeltmeler(json.dumps(bozuk), self.REV['digest'], self.iddialar,
                                                         self.gereksinimler, self.KRITERLER), [])
        celiski = json.dumps(govde) + '\n' + json.dumps({**govde, 'gereksinimler': []})
        self.assertEqual(rapor.somut_duzeltmeler(celiski, self.REV['digest'], self.iddialar, self.gereksinimler,
                                                 self.KRITERLER), [], 'celisen govdeler onarima aktarilmaz')


DENEME23 = Path(__file__).resolve().parent / 'fixtures/deneme23-20261018'


class Deneme23Test(unittest.TestCase):
    def setUp(self):
        self.paket = json.loads((DENEME23 / 'paket.json').read_text())
        self.raw = (DENEME23 / 'hakem1.txt').read_text()
        self.rev = {'digest': self.paket['revision']}
        self.ogeler = rapor.inceleme_ogeleri(self.paket['report_input'])
        self.gereksinimler = rapor.inceleme_gereksinimleri(self.paket['kabul'], self.paket['plan'])

    def uygula(self, raw):
        review = parse_review(raw, self.rev, self.paket['criteria'], planning=True)
        review = rapor.yoklama_uygula(raw, review, [k['id'] for k in self.paket['evidence']])
        return rapor.anlamsal_karar_uygula(raw, review, self.ogeler, self.paket['iddialar'], self.gereksinimler,
                                           self.paket['evidence'])

    def govde(self):
        import re
        blok = re.findall(r'```json\s*(\{.*?\})\s*```', self.raw, re.S)[-1]
        return json.loads(blok)

    def test_real_answer_is_parsed_without_a_protocol_error_and_evidence_count_is_closed_by_the_engine(self):
        mekanik = [g['id'] for g in self.gereksinimler if g['tur'] == 'mekanik']
        self.assertEqual(mekanik, ['R17', 'g3.5'])
        sonuc = self.uygula(self.raw)
        self.assertNotEqual(sonuc['status'], 'ERROR', sonuc.get('reason'))
        self.assertEqual({i['id'] for i in sonuc['iddialar']}, {o['id'] for o in self.ogeler if o['evidence_ids']})
        kararlar = {g['id']: g for g in sonuc['gereksinimler']}
        self.assertEqual({k: (kararlar[k]['durum'], kararlar[k]['rapor']) for k in mekanik},
                         {k: ('karsilandi', []) for k in mekanik})
        self.assertTrue(kararlar['R17']['gerekce'].startswith('motor kanit eki denetimi'))
        self.assertEqual({k: kararlar[k]['durum'] for k in self.paket['operator']['karsilanmadi']},
                         dict.fromkeys(self.paket['operator']['karsilanmadi'], 'karsilandi'),
                         'bicim gecerli; hakemin bu icerik kararlari operator denetiminde reddedildi')

    def test_operator_content_verdict_on_the_same_report_is_a_rejection_for_the_one_repair(self):
        govde = self.govde()
        govde['verdict'] = 'DUZELT'
        for karar in govde['gereksinimler']:
            if karar['id'] in self.paket['operator']['karsilanmadi']:
                karar.update(durum='eksik', rapor=[], gerekce='somut degerler raporda yok: degisken adlari, "local", len(lines) == 3, sira')
        for karar in govde['iddialar']:
            if karar['id'] == 'Bulgular-8':
                karar.update(destek='kismi', gerekce='checksum uygulamasi okunmadi; gorunen kontrol initialize() == ()')
        sonuc = self.uygula(json.dumps(govde, ensure_ascii=False))
        self.assertEqual((sonuc['status'], sonuc['verdict']), ('FAIL', 'DUZELT'))
        kanit = {a['id']: a['evidence'] for a in sonuc['acceptance'] if a['status'] == 'FAIL'}
        self.assertIn('Bulgular-8 destek=kismi', kanit['iddia_destegi'])
        for kimlik in self.paket['operator']['karsilanmadi']:
            self.assertIn(f'{kimlik} eksik', kanit['istek_kapsami'])

    def test_word_warnings_do_not_catch_the_real_overclaim_and_missing_term_decisions_do_not_decide_support(self):
        paket = {i['id']: i for i in self.paket['iddialar']}
        self.assertEqual(paket['Bulgular-8']['kanitta_olmayan_terimler'], [], 'checksum asiri cikarimi sozcuk uyarisi uretmez')
        govde = self.govde()
        for karar in govde['iddialar']:
            karar['terimler'] = []
        sonuc = self.uygula(json.dumps(govde, ensure_ascii=False))
        self.assertNotEqual(sonuc['status'], 'ERROR', sonuc.get('reason'))
        self.assertEqual({i['id']: i['destek'] for i in sonuc['iddialar']}, {k['id']: k['destek'] for k in govde['iddialar']})

    def test_incomplete_real_answer_never_passes(self):
        govde = self.govde()
        govde['iddialar'] = govde['iddialar'][:-1]
        self.assertEqual(self.uygula(json.dumps(govde, ensure_ascii=False))['status'], 'ERROR')
        govde = self.govde()
        govde['gereksinimler'] = [g for g in govde['gereksinimler'] if g['id'] != 'R3']
        self.assertEqual(self.uygula(json.dumps(govde, ensure_ascii=False))['status'], 'ERROR')

    def test_only_identical_plan_clauses_are_deduplicated_and_count_rule_allows_expected_assert_values(self):
        plan = {'gorevler': [{'id': 'g1', 'kabul': 'Kaynak değişmez; RC-OSS-005 DEVAM olarak belirtilir'},
                             {'id': 'g2', 'kabul': 'kaynak degismez.; En fazla 5 dosya:satir kaniti kullanilir'}]}
        ids = [g['id'] for g in rapor.inceleme_gereksinimleri('Kaynak değişmez; RC-OSS-005 DEVAM kalır', plan)]
        self.assertEqual(ids, ['R1', 'R2', 'g1.2', 'g2.2'])
        self.assertEqual([g['id'] for g in self.gereksinimler if g['kriter'] != 'request'][:3], ['g1.1', 'g1.2', 'g1.3'])
        self.assertIn('g3.6', [g['id'] for g in self.gereksinimler], 'benzer ama farkli plan sarti korunur')
        self.assertIn('len(lines) == 3', rapor.ANLAMSAL_HAKEM_KURALI)
        import kabul
        self.assertIn('len(lines) == 3', kabul.YAZAR_KANIT_KURALI)
        self.assertNotIn('sayi veya satir bilgisinin', kabul.YAZAR_KANIT_KURALI)

    def test_mechanical_clause_classifier_is_narrow(self):
        self.assertTrue(rapor.mekanik_maddesi('En çok 5 dosya:satır kanıtı kullanılır, komut kanıtı verilmez'))
        self.assertTrue(rapor.mekanik_maddesi('en fazla 5 dosya:satir kaniti kullanilir'))
        self.assertFalse(rapor.mekanik_maddesi('En fazla 5 dosya:satır kanıtı kullanılır ve her bulgu kanıtlı olur'))
        self.assertFalse(rapor.mekanik_maddesi('Test adedi veya sayımı yazılmaz'))
        fazla = [{'id': f'K{n}', 'tur': 'dosya'} for n in range(6)] + [{'id': 'K9', 'tur': 'komut'}]
        karar = rapor.mekanik_karar({'id': 'R17', 'metin': 'En çok 5 dosya:satır kanıtı kullanılır, komut kanıtı verilmez'}, fazla)
        self.assertEqual(karar['durum'], 'eksik')
        self.assertIn('6>5', karar['gerekce'])
        self.assertIn('K9', karar['gerekce'])


class KaynakBolumuQaTest(unittest.TestCase):
    """N-IN04: evidence given for a selected source section cannot spill outside the section's lines."""

    def test_evidence_on_a_section_restricted_file_must_stay_inside_the_selected_lines(self):
        from kosu import plan_product
        t = Path(tempfile.mkdtemp(prefix='bolum-qa-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt, sha = depo(t)
        (wt / 'BACKLOG.md').write_text('### [x] OLD — Existing\nold body\n### [ ] NEXT — Pending\nnext body\n')
        git(wt, 'add', '.')
        git(wt, 'commit', '-qm', 'backlog')
        beyan = {'yol': 'INCELEME.md', 'kanit': 'INCELEME.kanit.jsonl', 'bolumler': []}
        for kaynak, beklenen, hata in (('BACKLOG.md:3-4', 'next body', False), ('BACKLOG.md:4', 'next body', False),
                                       ('BACKLOG.md:1-2', 'old body', True), ('BACKLOG.md:2-4', 'next body', True),
                                       ('BACKLOG.md', 'next body', True)):
            with self.subTest(kaynak=kaynak):
                kanit = json.dumps(K(1, 'dosya', kaynak, beklenen))
                sonuc = rapor.rapor_qa(wt, beyan, sha, t / 'gecici', metinler=('# Bulgular\n- x [K1].\n', kanit),
                                       baslangic=plan_product(wt), kaynak_salt_okur=True,
                                       kaynak_yollari=[wt / 'BACKLOG.md', wt / 'app.py'],
                                       kaynak_bolumleri={wt / 'BACKLOG.md': [(3, 4)]})
                bulunan = [b for b in sonuc['bulgular'] if b.startswith('bolum-kapsam')]
                self.assertEqual(bool(bulunan), hata, sonuc['bulgular'])


class PlanDenetimTest(unittest.TestCase):
    """G-R1: in a report job the plan may only touch the report + evidence appendix; a violation is a schema error (correction to the planner)."""
    def plan(self, *dosya_listeleri):
        return {'is_id': 'x', 'gorevler': [{'id': f'g{i}', 'tanim': 't', 'kabul': 'k', 'model': 'auto', 'zorluk': 2,
                                            'paralel_grup': 0, 'tahmin_dk': 5, 'dosyalar': list(d)}
                                           for i, d in enumerate(dosya_listeleri, 1)]}

    def extract(self, plan, beyan):
        t = Path(tempfile.mkdtemp(prefix='rapor-plan-'))
        self.addCleanup(shutil.rmtree, t, True)
        (t / 'run.json').write_text(json.dumps({'rapor': beyan}))
        return subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'extract'], input=json.dumps(plan), text=True,
                              capture_output=True, env={**os.environ, 'CADRE_RUN_DIR': str(t)}, timeout=20)

    def test_rapor_disi_dosya_listeleyen_plan_reddedilir(self):
        b = rapor.beyan(RAPOR_ISTEMI)
        self.assertIsNone(rapor.plan_denetle(self.plan(['rapor.kanit.jsonl'], ['rapor.md', 'rapor.kanit.jsonl']), b))
        self.assertIn('app.py', rapor.plan_denetle(self.plan(['rapor.md', 'app.py']), b))
        self.assertIn('raporu yazan', rapor.plan_denetle(self.plan(['rapor.kanit.jsonl']), b))
        cikti = self.extract(self.plan(['rapor.md', 'app.py']), b)
        self.assertEqual(cikti.returncode, 2)
        self.assertIn('SEMA: rapor kipi', cikti.stderr)
        self.assertEqual(self.extract(self.plan(['rapor.md', 'app.py']), None).returncode, 0, 'rapor disi iste kural yok')

    def test_serit_istemi_rapor_isinde_kanit_kuralini_tasir(self):
        plan = self.plan(['rapor.md', 'rapor.kanit.jsonl'])
        istem = {}
        for ad, beyan in (('rapor', rapor.beyan(RAPOR_ISTEMI)), ('kod', None)):
            t = Path(tempfile.mkdtemp(prefix='rapor-pf-'))
            self.addCleanup(shutil.rmtree, t, True)
            (t / 'run.json').write_text(json.dumps({'rapor': beyan}))
            istem[ad] = subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'gorev_pf', 'g1'], input=json.dumps(plan),
                                       text=True, capture_output=True, env={**os.environ, 'CADRE_RUN_DIR': str(t)}, timeout=20).stdout
        self.assertIn('RAPOR KIPI', istem['rapor'])
        self.assertIn('rapor.kanit.jsonl', istem['rapor'])
        self.assertNotIn('RAPOR KIPI', istem['kod'])

    def test_plan_ve_serit_istemleri_kanit_kurallarini_tasir(self):
        b = rapor.beyan(RAPOR_ISTEMI)
        self.assertIn('rapor.kanit.jsonl', rapor.plan_istemi(b))
        self.assertIn('[K', rapor.serit_kurallari(b))
        self.assertTrue(rapor.yazar_mi(self.plan(['rapor.kanit.jsonl'], ['rapor.md', 'rapor.kanit.jsonl']), 'g2', b))
        self.assertFalse(rapor.yazar_mi(self.plan(['rapor.kanit.jsonl'], ['rapor.md', 'rapor.kanit.jsonl']), 'g1', b))


class RaporKabulKapisiTest(unittest.TestCase):
    """G-R3/G-R5/G-R6 from the production path: qa_kapisi and finalize_run."""
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='rapor-kabul-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.base = depo(self.t)
        self.store = RunStore(self.t)
        (self.t / 'projects/a').mkdir(parents=True)
        (self.t / 'projects/a/qa-komut').write_text('exit 1')   # if project QA ran in a report job it would be red
        self.run = self.store.accept('a', RAPOR_ISTEMI, source_sha=self.base)
        self.rid = self.run['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        yazan = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/attempt.json'
        yazan.parent.mkdir(parents=True)
        yazan.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
        for ctx in (patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_GOREV_ID': '', 'CADRE_OWNER_FDS': ''}),
                    patch.object(core, 'QE', self.t), patch.object(core, 'worktree_base_sha', return_value=self.base),
                    patch.object(core, 'butce_json', return_value=None)):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)
        self.onarim = Mock(return_value=Mock(poll=lambda: 0, wait=lambda **k: 0, returncode=0))
        ctx = patch.object(core, 'run_opencode', self.onarim)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.log = self.t / 'motor.log'

    def test_rapor_kipinde_qa_kapisi_rapor_qa_kosar_proje_qa_kosmaz(self):
        kanit_yaz(self.wt, GECERLI_RAPOR, GECERLI_KANIT)
        sonuc = core.qa_kapisi('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual((sonuc['status'], sonuc.get('kind')), ('PASS', 'rapor'))
        self.assertEqual(sonuc['revision'], revision(self.wt))

    def test_rapor_qa_kirmizisinda_yerel_onarim_yok(self):
        kanit_yaz(self.wt, GECERLI_RAPOR + "Toplam 431 ilan.\n", GECERLI_KANIT)
        sonuc = core.qa_kapisi('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(sonuc['status'], 'FAIL')
        self.onarim.assert_not_called()
        self.assertIn('kanitsiz-sayi', Path(sonuc['attempts'][-1]['output']).read_text())

    def test_hakem_istemi_yoklama_protokolunu_tasir_ve_yanlis_duzelt_olur(self):
        kanit_yaz(self.wt, GECERLI_RAPOR, GECERLI_KANIT)
        istemler = []

        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            istemler.append(args[-1])
            veri = json.loads(args[-1].splitlines()[-1])
            return json.dumps({'revision': veri['revision'], 'verdict': 'GECER',
                               'acceptance': [{'id': c['id'], 'status': 'PASS', 'evidence': 'rapor okundu'} for c in veri['criteria']],
                               'yoklama': [{'id': 'K1', 'sonuc': 'dogrulandi', 'kanit': 'app.py:2 okundu'},
                                           {'id': 'K2', 'sonuc': 'yanlis', 'kanit': 'komut yesil bitti'}]})
        with patch.object(core, 'capture_opencode', side_effect=hakem):
            sonuc = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertIn('RAPOR YOKLAMASI', istemler[0])
        self.assertEqual(json.loads(istemler[0].splitlines()[-1])['rapor']['kanit_idleri'], ['K1', 'K2'])
        self.assertEqual((sonuc['status'], sonuc['reason'], sonuc['review']['verdict']), ('FAIL', 'review-not-passed', 'DUZELT'))


class RaporDuzeltmeIstemiTest(unittest.TestCase):
    def test_duzeltme_istemi_rapor_bulgularini_ve_kanit_kuralini_tasir(self):
        t = Path(tempfile.mkdtemp(prefix='rapor-duz-'))
        self.addCleanup(shutil.rmtree, t, True)
        (t / 'qa.log').write_text('RAPOR QA FAIL: 1 bulgu\nkanitsiz-sayi: rapor.md:2: 431 ilan elendi\n')
        (t / 'istem.md').write_text(RAPOR_ISTEMI)
        kayit = {'rapor': rapor.beyan(RAPOR_ISTEMI), 'prompt': str(t / 'istem.md'),
                 'final_result': {'status': 'FAIL', 'reason': 'qa-not-passed', 'qa': {'attempts': [{'output': str(t / 'qa.log')}]}}}
        metin = core.duzeltme_istemi(kayit, 1)
        self.assertIn('RAPOR QA', metin)
        self.assertIn('kanitsiz-sayi: rapor.md:2', metin)
        self.assertIn('rapor.kanit.jsonl', metin)
        self.assertNotIn('projenin test komutu', metin)


class RaporCalistirmaTest(unittest.TestCase):
    def test_guclu_model_yoksa_rapor_isi_sade_motora_dusmez(self):
        """G-R6: when the dispatcher skips (no plan/budget) a report job is not handed to the local single-lane path."""
        t = Path(tempfile.mkdtemp(prefix='rapor-rc3-'))
        self.addCleanup(shutil.rmtree, t, True)
        root = t / 'code'
        (root / 'bin').mkdir(parents=True)
        (root / 'bin/cadre-dispatch.sh').write_text('echo dispatch >> "$CADRE_RUN_DIR/calls"\nexit 3\n')
        (root / 'bin/is-motoru.sh').write_text('echo motor >> "$CADRE_RUN_DIR/calls"\nexit 0\n')
        store = RunStore(t)
        run = store.accept('a', RAPOR_ISTEMI)
        env = {**os.environ, 'CADRE_HOME': str(t), 'CADRE_ENGINE_ROOT': str(root)}
        rc = subprocess.run([sys.executable, str(LIB / 'kosu.py'), 'run', run['run_id']], env=env, timeout=20).returncode
        kayit = store.read(run['run_id'])
        self.assertEqual((store.path(run['run_id']) / 'calls').read_text().split(), ['dispatch'])
        self.assertNotEqual(rc, 0)
        self.assertEqual(kayit['state'], 'retryable')
        self.assertTrue(kayit['reason'].startswith('butce-bekleme'), kayit['reason'])
        self.assertTrue(kayit.get('retry_after'))


RAPOR_ISCI = """
import json, os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
gid = os.environ['CADRE_GOREV_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
with open(os.environ['FIXTURE_MODELLER'], 'a') as f:
    f.write(gid + '=' + (os.environ.get('CADRE_RUN_MODEL') or 'auto') + '\\n')
with open(os.environ['FIXTURE_MODELLER'] + '.ekserit', 'a') as f:   # B226: ek serit surerken serit cebi devredilmez
    f.write(gid + ':' + ('var' if (RunStore().path(os.environ['CADRE_RUN_ID']) / 'ek-serit.flag').exists() else 'yok') + '\\n')
with open(os.environ['FIXTURE_MODELLER'] + '.once', 'a') as f:   # G-R7 bagimsizlik: serit basinda raporun durumu
    f.write(gid + ':' + ('var' if (wt / 'rapor.md').exists() else 'yok') + '\\n')
kanit = [{'id': 'K1', 'iddia': 'cikarma', 'tur': 'dosya', 'kaynak': 'app.py:2', 'beklenen': 'return a - b'}]
(wt / 'rapor.kanit.jsonl').write_text(''.join(json.dumps(k) + '\\n' for k in kanit))
satir = 'topla cikarma yapiyor [K1].' if gid.startswith('duzeltme') or gid.endswith('mutabakat') or os.environ.get('FIXTURE_YAZAR_DUS') else 'topla cikarma yapiyor; 431 ilan etkilendi.'
(wt / 'rapor.md').write_text('# Ozet\\n' + satir + '\\n')
if os.environ.get('FIXTURE_YAZAR_DUS') and gid == 'g1':   # B238: urun diskte, ama yazar seridi dusuyor (dongu/bekci kesmesi)
    raise SystemExit(1)
if os.environ.get('FIXTURE_MUTABAKAT_DUS') and gid.endswith('mutabakat'):   # B256: uzlastirici butce reddi/kesinti ile dusuyor, ciktisi yok
    (wt / 'rapor.md').unlink(); raise SystemExit(1)
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='uretti')
"""

SAHTE_HAKEM = """
import os, sys, json
calls = os.path.join(os.path.dirname(sys.argv[0]), 'reviewer-calls')
if not os.environ['CADRE_GOREV_ID'].startswith('plan-review-'): open(calls, 'a').write('x')
q = json.loads(sys.argv[-1].splitlines()[-1])
answer = {'verdict': 'GECER', 'revision': q['revision'],
          'acceptance': [{'id': i['id'], 'status': 'PASS', 'evidence': 'rapor okundu, kanit yoklandi'} for i in q['criteria']]}
r = q.get('rapor') or {}
answer['yoklama'] = [{'id': k, 'sonuc': 'dogrulandi', 'kanit': k + ' kaynagindan okundu'} for k in (r.get('kanit_idleri') or [])[:r.get('en_az', 0)]]
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + os.environ['CADRE_GOREV_ID'], 'part': {'type': 'text', 'text': json.dumps(answer)}}))
"""


class RaporUctanUcaTest(unittest.TestCase):
    """Real dispatcher + coordinator; a fake worker deliberately writes an invented number (the design's evidence plan)."""
    GUCLU = 'deepseek/deepseek-v4-pro'

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='rapor-e2e-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.base = depo(self.t)
        self.store = RunStore(self.t)
        (self.t / 'projects/a').mkdir(parents=True)
        (self.t / 'projects/a/qa-komut').write_text('exit 1')
        self.rid = self.store.accept('a', RAPOR_ISTEMI, source_sha=self.base)['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        yazan = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/attempt.json'
        yazan.parent.mkdir(parents=True)
        yazan.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))

    def kos(self, guclu, plan_ek=None, env_ek=None):
        root = self.t / 'dispatcher'
        from qa_fixture import instructions
        instructions(root)
        (root / 'lib').mkdir(parents=True)
        (root / 'bin').mkdir()
        for name in ('arsiv.py', 'kosu.py', 'sonuc.py', 'oturum.py', 'qe_core.py', 'canlilik.py', 'kabul.py', 'motor.py', 'dispatch_util.py', 'butce_kapisi.py', 'go_pencere.py', 'sinirlar.py', 'sinirlar.json', 'sinirlar.mjs', 'opencode_adaptor.py', 'butce-plugin.mjs', 'cadre-plugin.mjs', 'aylik-butce-plugin.mjs',
                     'yonlendirici.py', 'model_secimi.py', 'katalog.py', 'sozlesme.py', 'seviye.py', 'onelek.py', 'bolme.py', 'rapor.py',
                     'standart-gorev-formu.txt'):
            shutil.copyfile(LIB / name, root / 'lib' / name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        yon = (root / 'lib/yonlendirici.py').read_text()   # only the fixture copy: imitating a strong plan link
        yon = yon.replace('if __name__ == "__main__":\n    a = sys.argv[1:]\n',
                          'if __name__ == "__main__":\n    a = sys.argv[1:]\n'
                          '    _z = [m for m in os.environ.get("FIXTURE_GUCLU", "").split(",") if m]\n'
                          '    if a[:1] == ["ilk"] and a[1:2] and a[1] in ("plan", "yazar") and "FIXTURE_GUCLU" in os.environ:\n'
                          '        print(_z[0] if _z else ""); sys.exit(0)\n'
                          '    if a[:1] == ["sonraki"] and a[1:2] and a[1] in ("plan", "yazar") and _z:\n'
                          '        print(_z[_z.index(a[2]) + 1] if a[2] in _z[:-1] else ""); sys.exit(0)\n', 1)
        (root / 'lib/yonlendirici.py').write_text(yon)
        import test_uctan_uca
        from test_kosu_sessions import KATALOG
        (root / 'lib/butce.py').write_text(test_uctan_uca.FAKE_BUTCE)
        (self.t / 'models.json').write_text(json.dumps(KATALOG))
        shutil.copyfile(LIB.parent / 'bin/cadre-dispatch.sh', root / 'bin/cadre-dispatch.sh')
        (root / 'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n')
        (root / 'worker.py').write_text(RAPOR_ISCI)
        (root / 'bin/is-motoru.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n')
        plan = {'is_id': 'fixture', 'gorevler': [{'id': 'g1', 'tanim': 'raporu yaz', 'model': 'auto', 'paralel_grup': 0,
                                                  'dosyalar': ['rapor.md', 'rapor.kanit.jsonl'], 'kabul': 'kanitli rapor',
                                                  'zorluk': 2, 'tahmin_dk': 1}]}
        plan.update(plan_ek or {})
        (self.store.path(self.rid) / 'plan.json').write_text(json.dumps(plan))
        self.store.update(self.rid, phase='dispatch')
        hakem = self.t / 'fake-reviewer'
        hakem.write_text(f'#!{sys.executable}\n' + SAHTE_HAKEM)
        hakem.chmod(0o700)
        env = {'CADRE_ENGINE_ROOT': str(root), 'CADRE_REAL_OPENCODE': str(hakem), 'OPENCODE_DB': str(self.t / 'absent.db'),
               'FIXTURE_MODELLER': str(self.t / 'modeller'), 'FIXTURE_GUCLU': guclu, 'CADRE_BEKLEME_ESIK_SN': '0', 'CADRE_HOME': str(self.t),
               'CADRE_MODEL_KATALOG': str(self.t / 'models.json'), **(env_ek or {})}   # P3: no local, the cloud has a fake budget+catalogue
        with patch.dict(os.environ, env):
            rc = execute_run(self.store, self.rid)
        modeller = (self.t / 'modeller').read_text().split() if (self.t / 'modeller').exists() else []
        return rc, self.store.read(self.rid), modeller

    def test_uydurma_sayi_qa_ile_yakalanir_ve_duzeltme_turu_guclu_modelle_kapatir(self):
        rc, kayit, modeller = self.kos(self.GUCLU)
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result'))
        self.assertEqual(modeller, [f'g1={self.GUCLU}', f'duzeltme-1={self.GUCLU}'], 'G-R6: yazar ve duzeltme turu guclu modelde')
        self.assertEqual([d['imza'][:3] for d in kayit.get('duzeltme') or []], ['qa:'])
        self.assertEqual(kayit['final_result']['qa'].get('kind'), 'rapor')
        self.assertTrue(kayit['final_result']['review'].get('yoklama'), 'hakem yoklamasi kayitta')

    def test_zor_raporda_iki_bagimsiz_analist_ve_mutabakat(self):
        """G-R7: in a hard+ report the second analyst writes without seeing the first one's draft; a third model reconciles the divergences."""
        (self.store.path(self.rid) / 'ek-serit.flag').write_text('onceki dagitici G-R7 ortasinda oldu\n')   # stale marker
        rc, kayit, modeller = self.kos('m/a,m/b,m/c', plan_ek={'kapsam_puani': 50})
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result'))
        self.assertEqual(modeller, ['g1=m/a', 'g1-analist-b=m/b', 'g1-mutabakat=m/c'])
        self.assertEqual((self.t / 'modeller.ekserit').read_text().split(), ['g1:yok', 'g1-analist-b:var', 'g1-mutabakat:var'])
        self.assertFalse((self.store.path(self.rid) / 'ek-serit.flag').exists(), 'isaret G-R7 sonunda kalkar')
        self.assertIn('g1-analist-b:yok', (self.t / 'modeller.once').read_text(), 'ikinci analist ilk taslagi gordu')
        for ad in ('analist-a', 'analist-b'):
            self.assertTrue((self.wt / '.cadre-plan' / ad / 'rapor.md').is_file(), ad)
        istem = (self.store.path(self.rid) / 'tasks/g1-mutabakat/prompt.md').read_text()
        self.assertIn('.cadre-plan/analist-a/rapor.md', istem)
        self.assertIn('.cadre-plan/analist-b/rapor.kanit.jsonl', istem)
        self.assertEqual(kayit['final_result']['qa']['status'], 'PASS')
        analist = kayit['rapor']['analist']
        self.assertEqual((analist['kip'], analist['birinci'], analist['ikinci'], analist['uzlastirici']), ('mutabakat', 'm/a', 'm/b', 'm/c'))
        self.assertIn('Analist notu (G-R7): iki bagimsiz analist', (self.wt / 'rapor.md').read_text())

    def test_mutabakat_dusunce_atilan_seritler_isaretlenir(self):
        """B256 (26.09 live EK1): when the reconciler failed the dispatcher went back to the first analyst's report; the second analyst's and the reconciler's
        output was NOT in the product but their models counted as 'writers' and exhausted the judge pool. A discarded lane gets `atildi.flag`."""
        rc, kayit, modeller = self.kos('m/a,m/b,m/c', plan_ek={'kapsam_puani': 50}, env_ek={'FIXTURE_MUTABAKAT_DUS': '1'})
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result'))
        self.assertIn('mutabakat uretemedi', (self.store.path(self.rid) / 'dispatch.log').read_text())
        for gid in ('g1-analist-b', 'g1-mutabakat'):
            self.assertTrue((self.store.task_dir(self.rid, gid) / 'atildi.flag').is_file(), f'{gid}: ciktisi atilan serit isaretlenir')
        self.assertFalse((self.store.task_dir(self.rid, 'g1') / 'atildi.flag').exists(), 'ilk analist urunde kaldi')
        self.assertEqual((kayit['rapor']['analist']['kip'], kayit['rapor']['analist']['neden']), ('tek', 'mutabakat uretemedi'))
        self.assertEqual((self.wt / 'rapor.md').read_text().count('Analist notu (G-R7): tek analist'), 1, 'duzeltme turu notu silse de QA oncesi tek satir')

    def test_yeniden_onayda_uretilmis_mutabakat_tekrar_kosmaz(self):
        """B258 (26.09 live EK1): when a report job came back for re-approval (`--devam`/retry) the dispatcher ran the G-R7 lanes again from scratch
        even though their outputs were `uretti` (~$0.3 + 5 min every time). If the reconciler is already `uretti` (or discarded) G-R7 is skipped."""
        for gid in ('g1-analist-b', 'g1-mutabakat'):
            self.store.task_update(self.rid, gid, state='uretti')
        rc, kayit, modeller = self.kos('m/a,m/b,m/c', plan_ek={'kapsam_puani': 50})
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result'))
        self.assertEqual(modeller[0], 'g1=m/a')
        self.assertFalse([m for m in modeller if 'analist-b' in m or 'mutabakat' in m], f'G-R7 seritleri yeniden kosmadi: {modeller}')
        self.assertIn('B258', (self.store.path(self.rid) / 'dispatch.log').read_text())

    def test_ikinci_analist_yoksa_tek_analistle_surer(self):
        """N-REP03 (CR-30): without a strong second model a single analyst is written explicitly in the run record and in the report itself."""
        rc, kayit, modeller = self.kos('m/a', plan_ek={'kapsam_puani': 50})
        self.assertEqual(modeller[0], 'g1=m/a')
        self.assertNotIn('g1-analist-b', ' '.join(modeller))
        self.assertIn('G-R7', (self.store.path(self.rid) / 'dispatch.log').read_text())
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result'))
        analist = kayit['rapor']['analist']
        self.assertEqual((analist['kip'], analist['birinci'], analist['ikinci'], analist['neden']), ('tek', 'm/a', None, 'ikinci guclu analist yok'))
        self.assertIn('Analist notu (G-R7): tek analist; ikinci bagimsiz analist calismadi (ikinci guclu analist yok).', (self.wt / 'rapor.md').read_text())

    def test_yazar_dusse_de_diskteki_urun_kapidan_geciyorsa_kurtarilir(self):
        """B238 (26.09 live R13-v4): after writing the report the Go writer read its own report 9 times and looped, the guard cut it,
        the lane became 'basarisiz' and a 12 KB report + a 19 KB evidence appendix were thrown away. When the chain is exhausted, if the product on disk passes
        report QA the lane counts as produced; otherwise the old behaviour (failed)."""
        rc, kayit, modeller = self.kos(self.GUCLU, env_ek={'FIXTURE_YAZAR_DUS': '1'})
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result'))
        self.assertIn('KURTARILDI', (self.store.path(self.rid) / 'dispatch.log').read_text())
        self.assertEqual(modeller[0], f'g1={self.GUCLU}')
        self.assertNotIn('auto', ' '.join(modeller), 'kurtarma yonlendiriciye birakilmaz, guclu modelde kosar')

    def test_guclu_model_yokken_rapor_yazari_kosmaz(self):
        rc, kayit, modeller = self.kos('')
        self.assertNotEqual(rc, 0)
        self.assertEqual(modeller, [], 'G-R6: rapor yazari guclu model olmadan kosmadi')
        self.assertIn('rapor yazari', (self.store.path(self.rid) / 'dispatch.log').read_text())


if __name__ == '__main__':
    unittest.main()
