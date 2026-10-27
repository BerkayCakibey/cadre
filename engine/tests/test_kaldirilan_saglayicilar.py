"""4c-1/11: direct DeepSeek, Cerebras, direct OpenAI, Mistral, Gemini and a second Groq account were removed; no code path, constant or
endpoint address remains (Groq only in the pre-filter, direct HTTP).
'deepseek' as a model family/prefix (DeepSeek models on Go/OR) and the OR tier tag 'openai' are not providers."""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / 'lib'))
import aylik_butce  # noqa: E402
import butce_kapisi  # noqa: E402
import katalog  # noqa: E402
import plan_ds  # noqa: E402
import qe_core  # noqa: E402
import yonlendirici as yon  # noqa: E402

KALDIRILAN = yon.KALDIRILAN_SAGLAYICILAR
MESRU = re.compile(r"[sS]tarts[wW]ith\(['\"]deepseek['\"]\)|for a in \('deepseek', 'qwen'|^KALDIRILAN_SAGLAYICILAR = ")   # model prefix / family name / definition


def saglayici(route: str) -> str:
    return str(route).split('|')[0].split('/', 1)[0]


class KodYoluTests(unittest.TestCase):
    def test_saglayici_sabitlerinde_sokulen_yok(self):
        kumeler = {
            'PAID': butce_kapisi.PAID, 'FREE': butce_kapisi.FREE, 'GUNLUK_ISTEK': butce_kapisi.GUNLUK_ISTEK,
            'SAGLAYICI': yon.SAGLAYICI, 'TAVAN_TIPI': yon.TAVAN_TIPI,
            'GOZLEM_ANAHTARI': yon.GOZLEM_ANAHTARI, 'ODENMIS': yon.ODENMIS, 'USD_SAGLAYICILAR': qe_core.USD_SAGLAYICILAR,
            'HAKEM_MALIYET_SIRASI': qe_core.HAKEM_MALIYET_SIRASI, 'aylik PROVIDERS': aylik_butce.PROVIDERS,
            'katalog KIMLIK': katalog.KIMLIK, 'plan_ds ENDPOINTS': plan_ds.ENDPOINTS,
        }
        for ad, kume in kumeler.items():
            with self.subTest(sabit=ad):
                self.assertFalse(set(kume) & set(KALDIRILAN), ad)
        rotalar = {*yon.KALITE_KATI, *yon.PAHALI, *yon.KISA.values(), *(r for z in yon.ROLLER.values() for r in z)}
        self.assertFalse({saglayici(r) for r in rotalar if r} & set(KALDIRILAN), 'zincir/kisa ad/kat rotasi')

    def test_eklenti_kumelerinde_sokulen_yok(self):
        for dosya in ('butce-plugin.mjs', 'aylik-butce-plugin.mjs'):
            metin = (ENGINE / 'lib' / dosya).read_text()
            for kume in re.findall(r"new Set\(\[([^\]]*)\]\)", metin):
                with self.subTest(dosya=dosya, kume=kume):
                    self.assertFalse(set(re.findall(r"'([\w-]+)'", kume)) & set(KALDIRILAN))

    def test_kodda_sokulen_saglayicinin_gozlem_anahtari_okunmuyor(self):
        """07.10 live (16b): the dispatcher still read ['ds']['oran'] from the budget observation (a KeyError trace); the identity guard
        only looked for the provider name. The observation keys of removed providers are not read in the code either."""
        anahtar = re.compile(r"['\"](ds|oa|gemini|cerebras|mistral)['\"]")   # after 16b: the screen summary also carried ('DS', 'ds')
        bulgular = []
        for yol in [*(ENGINE / 'lib').glob('*.py'), *(ENGINE / 'lib').glob('*.mjs'), *(ENGINE / 'bin').iterdir()]:
            if yol.is_file():
                bulgular += [f'{yol.name}:{no}' for no, satir in enumerate(yol.read_text(errors='replace').splitlines(), 1)
                             if anahtar.search(satir) and not MESRU.search(satir)]
        self.assertEqual(bulgular, [])

    def test_kodda_saglayici_kimligi_ve_uc_adresi_kalmadi(self):
        kimlik = re.compile(r"(^|[^/\w.-])['\"](" + '|'.join(KALDIRILAN) + r")['\"]")
        rota = re.compile(r"['\"](" + '|'.join(k for k in KALDIRILAN if k != 'openai') + r")/")
        adres = re.compile(r"api\.(" + '|'.join(KALDIRILAN) + r")\.")
        kokler = [*(ENGINE / 'lib').glob('*.py'), *(ENGINE / 'lib').glob('*.mjs'), *(ENGINE / 'bin').iterdir(),
                  *(ENGINE.parent / 'config' / 'agents').glob('*.md')]
        bulgular = []
        for yol in kokler:
            goreli = str(yol.relative_to(ENGINE)) if yol.is_relative_to(ENGINE) else str(yol)
            if not yol.is_file():
                continue
            for no, satir in enumerate(yol.read_text(errors='replace').splitlines(), 1):
                if MESRU.search(satir):
                    continue
                if kimlik.search(satir) or rota.search(satir) or adres.search(satir):
                    bulgular.append(f'{goreli}:{no}: {satir.strip()[:100]}')
        self.assertEqual(bulgular, [])


if __name__ == '__main__':
    unittest.main()
