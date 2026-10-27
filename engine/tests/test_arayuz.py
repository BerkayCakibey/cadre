from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arayuz_veri  # noqa: E402
import dil  # noqa: E402
import proje_kayit  # noqa: E402
from aylik_butce import cap_micro, period  # noqa: E402
from kosu import RunStore  # noqa: E402
import test_uctan_uca as e2e  # noqa: E402

OR_SOL, GLM, DS = 'openrouter/openai/gpt-6.1-sol', 'opencode-go/glm-5.3', 'opencode-go/deepseek-v4-pro'

class SpendingWarningTests(unittest.TestCase):
    def test_warning_and_explicit_alarm_remain_distinct_readonly_in_both_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, store = Path(tmp), RunStore(tmp)
            rid = store.accept('fixture', 'spending warning')['run_id']
            for language in ('tr', 'en'):
                with patch.dict(os.environ, {'CADRE_LANG': language}):
                    dil.kur(language)
                    for progress in ('productive', 'unknown'):
                        store.update(rid, state='running', harcama_uyarisi={'kind': 'speed-warning', 'ts': time.time(),
                                     'neden': 'cash: high rate', 'ilerleme': progress})
                        record = store.read(rid)
                        before = (store.path(rid) / 'run.json').read_bytes()
                        warnings = arayuz_veri.kosu_uyarilari(home, record)
                        self.assertEqual([n['id'] for n in warnings], ['hiz-' + rid])
                        detail = arayuz_veri.operasyon_dugumu(home, record, {})
                        self.assertNotIn('spending-approval', str(detail))
                        self.assertIn('Spending rate warning' if language == 'en' else 'Harcama hızı uyarısı', str(detail))
                        self.assertIn(('Recorded progress' if progress == 'productive' else 'Progress unknown')
                                      if language == 'en' else ('Kanıtlı ilerleme' if progress == 'productive' else 'İlerleme belirsiz'), str(detail))
                        self.assertEqual(before, (store.path(rid) / 'run.json').read_bytes())
                    store.update(rid, harcama_alarmi={'ts': time.time(), 'neden': 'explicit stop'})
                    self.assertEqual({n['id'] for n in arayuz_veri.kosu_uyarilari(home, store.read(rid))}, {'hiz-' + rid, 'alarm-' + rid})
                    store.update(rid, harcama_uyarisi={'kind': 'speed-warning', 'ts': time.time() - 601})
                    self.assertEqual([n['id'] for n in arayuz_veri.kosu_uyarilari(home, store.read(rid))], ['alarm-' + rid])
                    store.update(rid, harcama_alarmi={})
            dil.kur('tr')


ORNEK = """
import asyncio, io, os, sys, tempfile
os.environ['CADRE_HOME'] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
from rich.console import Console
dil.kur('tr')
def metin(r, w):
    c = Console(width=w, record=True, file=io.StringIO())
    c.print(r)
    return c.export_text()
def gez(app, d):
    app.ayrinti_r(d, False)
    for c in d['cocuk']:
        gez(app, c)
async def bekle(pilot, app):
    for _ in range(100):
        if app.yuklendi and app.bolum('ajanlar')['cocuk']:
            return
        await pilot.pause(0.05)
async def yuklendi(pilot, app):
    for _ in range(200):
        await app.workers.wait_for_complete()
        if not app.yukleniyor and not app.bekleyen:
            return
        await pilot.pause(0.05)
async def kos(boy):
    app = arayuz.Cadre(ornek=True, benzetim=False)
    async with app.run_test(size=boy) as pilot:
        await bekle(pilot, app)
        await pilot.press('right')
        ust = app.secili['id']
        await pilot.press('1', 'enter')
        atla = '>'.join(app.kimlik(d) for d in app.zincir()[1:])
        await pilot.press('down', 'enter', 'enter', 'down', 'down', 'enter', 'enter', 'down')
        await pilot.pause()
        derin = '>'.join((d.get('proje') or d['ad']).split(' · ')[0][:12] for d in app.zincir()[1:]) + '|' + app.secili['ad'][:2]
        menu = app.query_one('#menu').display
        await pilot.press('enter')
        yaprak = len(app.yol)
        await pilot.press('o')
        await pilot.pause()
        okuyucu = type(app.screen).__name__
        await pilot.press('q', 'left', 'left', 'left', 'left', 'left')
        await pilot.pause()
        geri = f"{len(app.yol)}:{app.query_one('#menu').display}"
        gecmis = next(c for c in app.bolum('gorevler')['cocuk'] if c.get('id') == 'gecmis')['cocuk']
        alt = metin(app.liste_r(gecmis, 4, 11), 36)
        kayan = f"{'▲ 2 tane yukarıda' in alt}:{len(alt.splitlines()) <= 11}"
        await pilot.press('n')
        await pilot.pause()
        secenekler = [s['tur'] for s in app.screen.secenekler]
        await pilot.press('up', 'enter', *'yeniklasor', 'enter', 'x', 'ctrl+s')
        await pilot.pause()
        yeni = f"{app.screen.adim}:{app.screen.hedef['tur']}:{app.screen.klasor}"
        await pilot.press('escape', '5')
        while app.secili.get('id') != 'dil':
            await pilot.press('down')
        await pilot.press('enter')
        await yuklendi(pilot, app)
        ingilizce = f"{dil.DIL}:{'tasks running' in str(app.bas_r())}"
        gez(app, app.kok)
        app.ipucu_r()
        eksik = sorted(dil.EKSIK)
        while app.secili.get('id') != 'renk':
            await pilot.press('down')
        await pilot.press('enter')
        await pilot.pause()
        palet = f"{arayuz.R['ad']}:{dil.renk_oku()}:{'Edit money caps' in metin(app.aciklama_r(), 80)}"
        dil.renk_yaz('Gruvbox')
        arayuz.palet_kur('Gruvbox')
        for _ in range(16):
            app.sim.adim()
        app.kok_degistir(arayuz_veri.agac(app.home))
        teslim = sum(1 for c in app.bolum('gorevler')['cocuk'][-1]['cocuk'] if c.get('teslim'))
        dil.yaz('tr')
        return f'{ust}|{atla}|{derin}|{menu}|{yaprak}|{okuyucu}|{geri}|{kayan}|{secenekler[0]}:{secenekler[-1]}|{yeni}|{ingilizce}|{eksik}|{palet}|{teslim}'
for boy in ((160, 44), (100, 30)):
    print(asyncio.run(kos(boy)))
"""

GERCEK = """
import asyncio, contextlib, json, os, sys
os.environ.update(GIT_AUTHOR_NAME='fixture',GIT_AUTHOR_EMAIL='fixture@local',GIT_COMMITTER_NAME='fixture',GIT_COMMITTER_EMAIL='fixture@local',CADRE_RUN_ID='')
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
from kosu import RunStore
dil.kur('tr')
home = Path(sys.argv[2])
cagrilar = []
gercek_run = arayuz.subprocess.run
class Sonuc:
    returncode, stdout, stderr = 0, '✓ Görev kaydedildi: r1.', ''
def sahte(komut, **kw):
    if komut[0] == 'git':
        return gercek_run(komut, **kw)
    cagrilar.append({'komut': komut, 'cwd': kw.get('cwd'), 'izle': (kw.get('env') or {}).get('CADRE_IZLE'),
                     'kosu': (kw.get('env') or {}).get('CADRE_RUN_ID')})
    return Sonuc()
arayuz.subprocess.run = sahte
arayuz.Cadre.yenile = lambda self, *a: None
import ekran
tazeleme = []
ekran.arkada_tazele = lambda h: tazeleme.append(str(h))
store = RunStore(home)
aktif = store.accept('lab', 'notlar.py ekle')['run_id']
store.update(aktif, state='running')
async def kos():
    app = arayuz.Cadre(ornek=False, home=home)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bas = str(app.bas_r())
        app.gorev_gonder({'tur': 'proje', 'ad': 'lab'}, 'notlar.py ekle')
        await app.workers.wait_for_complete()
        app.gorev_gonder({'tur': 'yeni', 'yol': str(home / 'yeni-proje')}, 'ilk görev')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen,arayuz.Onay)
        assert not (home/'yeni-proje/.git').exists()
        await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05);await pilot.press(*'evet','enter');await pilot.pause()
        assert isinstance(app.screen,arayuz.Onay)
        assert not (home/'yeni-proje/.git').exists()
        await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05);await pilot.press(*'evet','enter');await pilot.pause()
        await app.workers.wait_for_complete()
        app.kok_degistir(arayuz_veri.agac(home, ajanlar=False))
        await pilot.press('2', 'x')
        await pilot.pause()
        onay = type(app.screen).__name__
        await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05)
        await pilot.press('enter')
        await pilot.pause()
        await pilot.press('i')
        await pilot.pause()
        izlenen = app.screen.run_id
        await pilot.press('q')
        app.olcum_tazele()
        ag_yok = os.environ.pop('CADRE_AG_YOK', None)
        app.olcum_tazele()
        app.olcum_tazele()
        os.environ['CADRE_AG_YOK'] = ag_yok or ''
    return onay, bas, izlenen
onay, bas, izlenen = asyncio.run(kos())
print(json.dumps({'onay': onay, 'bas': bas, 'aktif': aktif, 'izlenen': izlenen, 'cagrilar': cagrilar, 'tazeleme': tazeleme}))
"""


HATA = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
dil.kur('tr')
bildirimler, eylemler = [], []
def bozuk(*a, **k):
    raise ValueError('bozuk kayıt')
arayuz_veri.agac = bozuk
async def kos():
    app = arayuz.Cadre(ornek=False, home=Path(sys.argv[2]))
    app.notify = lambda m, **kw: bildirimler.append(str(m))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        app.yenile(True)
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        ayakta = app.is_running
        ekranlar = []
        for tus in ('q', 'escape'):
            app.push_screen(arayuz.Onay('soru', 'ayrıntı', lambda: eylemler.append(tus)))
            await pilot.pause()
            await pilot.press(tus)
            await pilot.pause()
            ekranlar.append(type(app.screen).__name__)
        await pilot.press('n')
        await pilot.pause()
        await pilot.press('enter', 'ctrl+s')
        await pilot.pause()
        adim = app.screen.adim
    return ayakta, ekranlar, adim
ayakta, ekranlar, adim = asyncio.run(kos())
print(json.dumps({'ayakta': ayakta, 'ekranlar': ekranlar, 'eylemler': eylemler, 'adim': adim, 'bildirimler': bildirimler}))
"""


PENCERE = """
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, dil
dil.kur('tr')
home = Path(sys.argv[2])
for i in range(20):
    (home / 'repolar' / f'p{i:02d}').mkdir(parents=True)
    (home / 'projects' / f'p{i:02d}').mkdir(parents=True)
    (home / 'projects' / f'p{i:02d}' / 'repo-path').write_text(str(home / 'repolar' / f'p{i:02d}'))
arayuz.Cadre.yenile = lambda self, *a: None
def gorunur(app, boy):
    r = app.screen.query_one('#yg-ipucu').region
    return r.height > 0 and r.bottom <= boy[1]
async def kos(boy):
    app = arayuz.Cadre(ornek=False, home=home)
    async with app.run_test(size=boy) as pilot:
        await pilot.pause()
        await pilot.press('n')
        await pilot.pause()
        ilk = gorunur(app, boy)
        for _ in range(15):
            await pilot.press('down')
        await pilot.pause()
        secili = app.screen.secenekler[app.screen.secim]['etiket']
        secili_gorunur = secili in app.export_screenshot()
        await pilot.press('enter')
        await pilot.pause()
        app.screen.query_one('#yg-metin').text = '\\n'.join(f'satır {i}' for i in range(40))
        await pilot.press('ctrl+s')
        await pilot.pause()
        onay = (app.screen.adim, gorunur(app, boy), 'satır daha' in app.export_screenshot().replace('&#160;', ' '))
    return [ilk, secili_gorunur, *onay]
print(json.dumps([asyncio.run(kos(boy)) for boy in ((80, 24), (60, 20))]))
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def dugum_metinleri(dugum: dict) -> list[str]:
    metinler = [dugum['ad'], dugum['ozet']]
    for tur, deger in dugum['ayrinti']:
        if tur == 'alan':
            metinler.append(str(deger[0]))
        elif tur in ('baslik', 'bolum', 'satir', 'soluk'):
            metinler.append(str(deger))
    for c in dugum['cocuk']:
        metinler += dugum_metinleri(c)
    return metinler


def bul(dugum: dict, *kimlikler: str) -> dict:
    for kimlik in kimlikler:
        dugum = next(c for c in dugum['cocuk'] if c.get('id') == kimlik)
    return dugum


def alanlar(dugum: dict) -> dict:
    return dict(deger for tur, deger in dugum['ayrinti'] if tur == 'alan')


class IstekOnayMetniTests(unittest.TestCase):
    def test_second_approval_shows_summary_first_then_labelled_exact_command(self):
        proposal = {'token': 'a' * 64, 'action': {'output': 'rapor', 'target_path': '/hedef', 'providers': ['openrouter'],
                                                  'duration_minutes': 40, 'budget_usd': '1.5',
                                                  'models': {'plan': ['openrouter/m'], 'review': ['openrouter/m'], 'impl': []}}}
        komut = "printf '%s' '{\"action\": 1}' | python3 kosu.py request-approve r1 c1"
        for language in ('tr', 'en'):
            with patch.dict(os.environ, {'CADRE_LANG': language}):
                dil.kur(language)
                ilk = arayuz_veri.istek_onay_metni('r1', proposal)
                ikinci = arayuz_veri.istek_onay_metni('r1', proposal, komut)
            self.assertNotIn('printf', ilk)
            self.assertNotIn('impl:', ilk, 'boş model havuzu gösterilmez')
            self.assertTrue(ikinci.startswith(ilk), 'okunur özet önce gelir')
            self.assertIn(komut, ikinci, 'sözleşmedeki tam komut korunur')
            ek = ikinci[len(ilk):]
            self.assertLess(ek.index('a' * 64), ek.index(komut))
            etiketler = [satir for satir in ek.splitlines() if satir and komut not in satir and 'a' * 64 not in satir]
            self.assertEqual(len(etiketler), 1, ek)
            self.assertIn(dil.t('Onay kimliği'), ek)
        dil.kur('tr')

    def test_request_task_shows_work_and_waiting_time_from_the_engine_clock(self):
        from kosu import atomic_json
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            rid = store.accept('fixture', 'istek saati')['run_id']
            record = store.read(rid)
            record.update(request={'duration_minutes': 10}, request_clock={'baslangic': time.time() - 900, 'aktif_sn': 300.0, 'oturum': None})
            atomic_json(store.path(rid) / 'run.json', record)
            saat = arayuz_veri.istek_saati(store.read(rid))
            self.assertEqual(round(saat['calisma_sn']), 300)
            self.assertAlmostEqual(saat['bekleme_sn'], 600, delta=5)
            self.assertIsNone(arayuz_veri.istek_saati({'run_id': rid}))

    def test_internal_plan_input_tasks_are_not_listed_as_plan_subtasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, store = Path(tmp), RunStore(tmp)
            rid = store.accept('fixture', 'plan alt görevleri')['run_id']
            store.update(rid, tasks={'g1': {'state': 'pending'}, 'plan-input-98f5b5d1c311': {'state': 'done'},
                                     'main': {'state': 'pending'}, 'g9': {'state': 'pending'}})
            plan = {'gorevler': [{'id': 'g1', 'tanim': 'kanıt dilimi'}]}
            dugumler = arayuz_veri.alt_gorev_dugumleri(home, store.read(rid), plan)
            self.assertEqual([n['id'] for n in dugumler], ['g1', 'g9'], 'plan dışı gerçek görev görünür, iç plan girdisi görünmez')


class ArayuzVeriTests(unittest.TestCase):
    def kur(self, home: Path, repo: Path) -> dict:
        for proje in ('lab', 'lab--pdeneme'):
            (home / 'projects' / proje).mkdir(parents=True)
            (home / 'projects' / proje / 'repo-path').write_text(str(repo))
        (repo / 'a.py').write_text('x = 1\n')
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'taban')
        taban = git(repo, 'rev-parse', 'HEAD')
        wt = home / 'wt'
        git(repo, 'worktree', 'add', '-q', '-b', 'cadre/deneme', str(wt))
        (wt / 'a.py').write_text('x = 2\n')
        (wt / 'docs').mkdir()
        (wt / 'docs/rapor.md').write_text('# Rapor\n\nözet satırı\n')
        git(wt, 'add', '.')
        git(wt, 'commit', '-qm', 'urun')
        store = RunStore(home)
        bitmis = store.accept('lab', 'bitmiş görev', source_sha=taban)['run_id']
        store.update(bitmis, state='completed', worktree=str(wt), budget_spent_usd=0.4, budget_cap_usd=2.0,
                     final_result={'status': 'PASS', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS', 'model': 'opencode-go/qwen3.8-max',
                                                                                          'verdict': 'GECER', 'independent': True}})
        rapor = store.accept('lab', 'mimari rapor', source_sha=taban)['run_id']
        store.update(rapor, state='completed', worktree=str(wt), teslim_merge='2026-10-08 10:00:00', final_result={'status': 'PASS'})
        kayit = json.loads((store.path(rapor) / 'run.json').read_text())
        kayit.update(mode='rapor', rapor={'yol': 'docs/rapor.md', 'bolumler': ['Özet']})
        (store.path(rapor) / 'run.json').write_text(json.dumps(kayit))
        normal = store.accept('lab', 'notlar.py ekle\nayrıntı')['run_id']
        store.update(normal, state='running', plan_modeli=GLM, budget_cap_usd=2.0)
        (store.path(normal) / 'plan.json').write_text(json.dumps({'gorevler': [
            {'id': f'g{i}', 'tanim': f'görev {i}', 'dosyalar': [f'{i}.py'], 'kabul': 'pytest', 'bagimli': [], 'zorluk': 2, 'model': 'ds-pro',
             'paralel_grup': 1, 'tahmin_dk': 5, 'tahmin_usd': 0.1} for i in (1, 2, 3)]}))
        store.task_update(normal, 'g1', state='uretti')
        store.task_update(normal, 'g2', state='running')
        (store.path(normal) / 'dispatch.log').write_text('2026-10-08 15:18:01 plan kabul: 3 gorev\n2026-10-08 15:20:00 gorev g1 uretti\n'
                                                        '2026-10-08 15:20:00 gorev g1 uretti\n')
        deneme = store.path(normal) / 'tasks/g2/attempts/a1'
        deneme.mkdir(parents=True)
        (deneme / 'attempt.json').write_text(json.dumps({'model': DS, 'role': 'impl', 'session_id': 'ses_1'}))
        (deneme / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in [
            {'type': 'reasoning', 'timestamp': '2026-10-08T12:20:17.829Z', 'part': {'text': 'önce testleri   okuyacağım'}},
            {'type': 'tool_use', 'timestamp': 1759926017829, 'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}}}},
            {'type': 'text', 'timestamp': '2026-10-08T12:21:00Z', 'part': {'text': '{"a": 1}'}}]) + '\nbozuk satır\n')
        cocuk = store.accept('lab--pdeneme', 'f1 birinci')['run_id']
        store.update(cocuk, state='implemented', final_result={'status': 'UYGULANDI', 'qa': {'status': 'PASS'}}, proje_ust='ust')
        store.task_update(cocuk, 'g1', state='uretti')
        son = store.accept('lab--pdeneme', 'son kabul')['run_id']
        store.update(son, state='running', proje_ust='ust')
        riskli = store.accept('lab--pdeneme', 'f4 riskli')['run_id']
        store.update(riskli, state='implemented', proje_ust='ust', final_result={
            'status': 'UYGULANDI', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS', 'verdict': 'GECER', 'model': GLM}})
        buyuk = store.accept('lab', 'büyük görev')['run_id']
        store.update(buyuk, state='running', project_active_feature='f2', budget_spent_usd=1.0, budget_cap_usd=2.0, project_budget_cap_usd=20.0)
        store.update(cocuk, budget_spent_usd=7.0)
        (store.path(buyuk) / 'proje.json').write_text(json.dumps({'durum': 'calisiyor', 'tavan_usd': 10, 'cocuklar': [
            {'ozellik': 'f1', 'kosu': cocuk}, {'ozellik': 'son-kabul', 'kosu': son}], 'ozellikler': [
            {'id': 'f1', 'baslik': 'birinci', 'durum': 'gecti', 'deneme': 1, 'kosu': [cocuk], 'bagimli': ['f0'], 'kapsam_puani': 40},
            {'id': 'f2', 'baslik': 'ikinci', 'durum': 'bekliyor', 'deneme': 0},
            {'id': 'f3', 'baslik': 'üçüncü', 'durum': 'takildi', 'deneme': 3},
            {'id': 'f4', 'baslik': 'dördüncü', 'durum': 'gecti', 'deneme': 1, 'kosu': [riskli], 'riskli': True}]}))
        (home / 'zincir').mkdir()
        con = sqlite3.connect(home / 'zincir/aylik-butce.db')
        con.execute('create table reservation(id text primary key, provider text not null, period text not null, created real not null, '
                    'reserved integer not null, actual integer, receipt text unique, metadata text not null)')
        for i, (saglayici, tutar) in enumerate((('openrouter', 1_000_000), ('opencode-go', 5_000_000))):
            con.execute('insert into reservation values (?,?,?,?,?,?,?,?)', (f'r{i}', saglayici, period(), 1.0, tutar, tutar, f'm{i}', '{}'))
        con.commit()
        con.close()
        return {'bitmis': bitmis, 'rapor': rapor, 'normal': normal, 'buyuk': buyuk}

    def agac(self, home: Path) -> tuple[dict, dict]:
        gorunum = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
                   'go': {'kendi': {'rolling': 10, 'weekly': 20, 'monthly': 95}, 'kullanilabilir': 3, 'pencere_acik': True},
                   'or': {'kalan': 12.5, 'kullanilabilir': 12.5}, 'zen': {'kalan': 4}, 'claude': {'used7': 50, 'tavan': 77}}

        def zincir(rol: str, **_: object) -> tuple[list, list]:
            return ([GLM], [f'{OR_SOL}: OK (pay var)', f'{OR_SOL}: dogrulanmis pay yok']) if rol == 'plan' else ([DS], [])
        arayuz_veri.temizle()
        with patch('ekran.butce_gorunumu', return_value=(gorunum, 30)), \
             patch('yonlendirici.ROLLER', {'plan': [OR_SOL, GLM], 'impl': [DS], 'impl-hafif': [DS]}), \
             patch('yonlendirici.zincir', side_effect=zincir), patch('yonlendirici.durum_oku', return_value={}), \
             patch('katalog.baglam', return_value=1048576), patch('katalog.fiyat', return_value={'input': 0.5, 'output': 1.5}), \
             patch('sonuc.approved', return_value=True):
            hizli = arayuz_veri.agac(home, ajanlar=False)
            return hizli, arayuz_veri.agac(home)

    def test_real_data_tree_follows_the_engine_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as r:
            home, repo = Path(t), Path(r)
            k = self.kur(home, repo)
            once = sorted(p.relative_to(home) for p in home.rglob('*') if '.git' not in p.parts)
            dil.kur('tr')
            hizli, kok = self.agac(home)
            projeler = arayuz_veri.projeler(home)
            self.assertEqual(bul(hizli, 'ajanlar')['cocuk'], [], 'ilk aşama model zincirini hesaplamaz')
            self.assertEqual([(b['id'], b['ad']) for b in kok['cocuk']],
                             [('panel', 'Panel'), ('gorevler', 'Görevler'), ('butce', 'Bütçe'), ('ajanlar', 'Ajanlar'), ('ayarlar', 'Ayarlar')])
            panel = bul(kok, 'panel')
            self.assertEqual([(c['id'], c.get('git')) for c in panel['cocuk']],
                             [('su-an', ['gorevler']), ('ozet', ['gorevler', 'gecmis']), ('dikkat', None), ('panel-butce', ['butce'])])
            gorevler = bul(kok, 'gorevler')
            self.assertEqual((sorted(c['id'] for c in gorevler['cocuk'][:-1]), gorevler['cocuk'][-1]['id']),
                             (sorted([k['buyuk'], k['normal']]), 'gecmis'), 'alt koşular üst listeye çıkmaz')
            normal = bul(gorevler, k['normal'])
            self.assertEqual([c['id'] for c in normal['cocuk']], ['plan', 'test', 'hakem', 'ajanlar', 'harcama', 'olaylar', 'operasyon', 'secim'])
            self.assertEqual([a for a, _ in [v for tur, v in normal['aciklama'] if tur == 'alan']],
                             ['Plan', 'Test', 'Hakem', 'Ajanlar', 'Harcama', 'Olaylar', 'Karar ve operasyon', 'Görevdeki model seçimi'], 'görev menüsü içindekileri anlatır')
            self.assertEqual([(c['id'], c['durum']) for c in bul(normal, 'plan')['cocuk']], [('g1', 'bitti'), ('g2', 'simdi'), ('g3', 'bekliyor')])
            kayit = [v for tur, v in bul(normal, 'plan', 'g2')['ayrinti'] if tur == 'kayit']
            self.assertEqual([s[6:] for s in kayit[0]], ['önce testleri okuyacağım', '(JSON çıktı · 8 karakter)'])
            self.assertRegex(kayit[1][0], r'^\d\d:\d\d bash +pytest -q$')
            self.assertEqual(normal['su_an'], 'Kodlama · g2 görev 2')
            self.assertEqual(next(v for tur, v in bul(normal, 'olaylar')['ayrinti'] if tur == 'kayit'),
                             ['2026-10-08 15:18:01 (Saat dilimi kayıtsız) plan kabul: 3 gorev',
                              '2026-10-08 15:20:00 (Saat dilimi kayıtsız) gorev g1 uretti'], 'aynı olay satırı bir kez, mutlak zaman korunur')
            buyuk = bul(gorevler, k['buyuk'])
            self.assertEqual([c['id'] for c in buyuk['cocuk']], ['plan', 'son-kontrol', 'ajanlar', 'harcama', 'olaylar', 'operasyon', 'secim'])
            self.assertEqual([(c['id'], c['durum']) for c in bul(buyuk, 'plan')['cocuk']],
                             [('f1', 'bitti'), ('f2', 'simdi'), ('f3', 'hata'), ('f4', 'bitti')])
            self.assertEqual([c['id'] for c in bul(buyuk, 'plan', 'f1')['cocuk']], ['plan', 'test'], 'parçada inceleme yok, son kontrolde')
            self.assertEqual(([c['id'] for c in bul(buyuk, 'plan', 'f4')['cocuk']], bul(buyuk, 'plan', 'f4', 'hakem')['ozet']),
                             (['plan', 'test', 'hakem'], 'Geçer'), 'riskli parçanın kendi hakem incelemesi görünür')
            self.assertEqual([c['id'] for c in bul(buyuk, 'son-kontrol')['cocuk']], ['plan', 'test', 'hakem'])
            self.assertEqual(alanlar(bul(buyuk, 'plan', 'f1'))['Bağımlı'], 'f0')
            self.assertEqual((bul(buyuk, 'harcama')['ozet'], alanlar(buyuk)['Harcama']), ('$8,00 / $20,00', '$8,00 / $20,00'),
                             'büyük görevin harcaması alt koşularla ve proje tavanıyla')
            self.assertEqual(dict(v for tur, v in bul(kok, 'butce')['baglam'] if tur == 'alan')['lab'], '$8,40', 'en çok harcayan alt koşuları sayar')
            gecmis = bul(gorevler, 'gecmis')
            bitmis = bul(gecmis, k['bitmis'])
            teslim = bul(bitmis, 'teslim')
            self.assertTrue(bitmis['teslim'] and teslim['teslim'])
            self.assertEqual((teslim['ozet'], alanlar(teslim)['Dal'], alanlar(teslim)['Değişiklik']), ('Teslim bekliyor', 'cadre/deneme', '2 dosya · +4 −1'))
            self.assertIn('-x = 1', arayuz_veri.dosya_oku(teslim['dosya'])[2])
            self.assertIn('Teslim bekliyor', bitmis['ozet'])
            rapor_gorevi = bul(gecmis, k['rapor'])
            rapor = bul(rapor_gorevi, 'rapor')
            self.assertEqual((rapor['dosya']['tur'], arayuz_veri.dosya_oku(rapor['dosya'])[2].splitlines()[0]), ('md', '# Rapor'))
            self.assertEqual(bul(rapor_gorevi, 'teslim')['ozet'], 'Merge edildi · 2026-10-08 10:00:00')
            self.assertIn('g1 · görev 1', arayuz_veri.dosya_oku(bul(normal, 'plan')['dosya'])[2])
            butce = bul(kok, 'butce')
            self.assertEqual((bul(butce, 'go')['ozet'], bul(butce, 'openrouter')['ozet'], bul(butce, 'nakit')['ozet']),
                             ('Aylık %95', '$12,50', f"$1,00 / {arayuz_veri.para(cap_micro() / 1e6)}"), 'nakit sınırına Go sayılmaz')
            dikkat = bul(panel, 'dikkat')
            self.assertEqual(bul(dikkat, 'dikkat-go')['git'], ['butce', 'go'])
            self.assertIn('Takılan parça · lab', [u['ad'] for u in dikkat['cocuk']])
            ajanlar = bul(kok, 'ajanlar')
            self.assertEqual([c['ad'] for c in ajanlar['cocuk']], ['Planlayıcı', 'Geliştirici', 'Ön değerlendirici'],
                             'yalnız yapılandırılmış ana roller görünür')
            self.assertEqual(bul(ajanlar, 'impl', 'impl-hafif')['ad'], 'Hafif geliştirme')
            self.assertTrue(all(b.get('aciklama') and b['aciklama'][0][0] == 'baslik' for b in kok['cocuk']), 'her bölümün açıklaması var')
            halkalar = bul(ajanlar, 'plan')['cocuk']
            self.assertEqual([(c['ad'], c['ozet'], alanlar(c)['Neden']) for c in halkalar],
                             [(f'1. {OR_SOL}', 'Elendi', 'dogrulanmis pay yok'), (f'2. {GLM}', 'İlk tercih', 'Zincirde, payı ve bağlamı uygun')])
            self.assertEqual((alanlar(halkalar[0])['Context'], alanlar(halkalar[0])['Fiyat']), ('1,05M token', 'Giriş $0,50 · çıkış $1,50 / 1M token'))
            self.assertEqual(alanlar(bul(ajanlar, 'on-degerlendirme'))['Model'], 'groq/openai/gpt-oss-120b')
            self.assertEqual((bul(kok, 'ayarlar', 'dil').get('eylem'), bul(kok, 'ayarlar', 'renk').get('eylem')), ('dil', 'renk'))
            self.assertEqual(projeler, ['lab'])
            self.assertFalse([m for m in dugum_metinleri(kok) if ';' in m], 'arayüz metninde noktalı virgül yok')
            self.assertFalse([k for k, v in {**dil.EN, **dil.TEKIL}.items() if ';' in k or ';' in v], 'dil tablosunda noktalı virgül yok (FIX-41)')
            dil.EKSIK.clear()
            dil.kur('en')
            try:
                _, en = self.agac(home)
            finally:
                dil.kur('tr')
            self.assertEqual((bul(en, 'gorevler')['ad'], sorted(dil.EKSIK)), ('Tasks', []))
            self.assertEqual(bul(en, 'gorevler', k['normal'])['aciklama'][0], ('baslik', 'This task'))
            dil.kur('en')
            try:
                self.assertEqual((dil.t('{n} görev', n=1), dil.t('{n} görev', n=2), dil.t('{f} dosya · +{e} −{s}', f=1, e=2, s=0)),
                                 ('1 task', '2 tasks', '1 file · +2 −0'))
            finally:
                dil.kur('tr')
            sonra = sorted(p.relative_to(home) for p in home.rglob('*') if '.git' not in p.parts and '__pycache__' not in str(p))
            self.assertEqual(sonra, once, 'arayüz hiçbir dosya yazmaz')


    def test_finished_task_delivery_is_rechecked_when_the_product_changes(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as r:
            home, repo = Path(t), Path(r)
            k = self.kur(home, repo)
            arayuz_veri.temizle()

            def onay(kayit: dict) -> bool:
                return (Path(kayit['worktree']) / 'a.py').read_text() == 'x = 2\n'
            with patch('sonuc.approved', side_effect=onay):
                ilk = bul(arayuz_veri.agac(home, ajanlar=False), 'gorevler', 'gecmis', k['bitmis'])
                (home / 'wt/a.py').write_text('x = 3\n')
                arayuz_veri.IZ.clear()
                sonra = bul(arayuz_veri.agac(home, ajanlar=False), 'gorevler', 'gecmis', k['bitmis'])
            self.assertEqual((bool(ilk.get('teslim')), bool(sonra.get('teslim'))), (True, False), 'ürün değişince teslim beklentisi düşer')

    def test_claude_thinking_text_and_tool_events_are_shown(self):
        with tempfile.TemporaryDirectory() as t:
            deneme = Path(t)
            olaylar_ = [{'type': 'system', 'subtype': 'init'},
                        {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'planı   okuyorum'},
                                                                      {'type': 'text', 'text': 'Dosyayı açıyorum'},
                                                                      {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pytest -q'}}]}},
                        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'a.py'}}]}},
                        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}}]
            (deneme / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in olaylar_) + '\nbozuk\n{"type": "assistant", "message": {"con')
            dusunce, arac = arayuz_veri.beyin(deneme)
        self.assertEqual(([x[6:] for x in dusunce], [x[6:] for x in arac]),
                         (['planı okuyorum', 'Dosyayı açıyorum'], ['Bash     pytest -q', 'Read     a.py']))

    def test_old_running_and_old_deliverable_tasks_stay_visible_beyond_recent_records(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            store = RunStore(home)
            eski = store.accept('lab', 'eski çalışan')['run_id']
            store.update(eski, state='running')
            (home / 'wt-eski').mkdir()
            teslim = store.accept('lab', 'eski teslim')['run_id']
            store.update(teslim, state='completed', worktree=str(home / 'wt-eski'), final_result={'status': 'PASS'})
            for i in range(160):
                store.update(store.accept('lab', f'yeni {i}')['run_id'], state='completed', final_result={'status': 'FAIL'})
            arayuz_veri.temizle()
            with patch('sonuc.approved', return_value=True):
                kok = arayuz_veri.agac(home, ajanlar=False)
        self.assertIn(eski, [c['id'] for c in bul(kok, 'gorevler')['cocuk']], '150 kayıttan eski çalışan görev listede kalır')
        self.assertIn(teslim, [c['id'] for c in bul(kok, 'panel', 'dikkat', 'dikkat-teslim')['cocuk']], 'eski teslim bekleyen Dikkat\'te')

    def test_one_broken_record_does_not_take_down_the_tree(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            store = RunStore(home)
            iyi = store.accept('lab', 'iyi görev')['run_id']
            store.update(iyi, state='running')
            bozuklar = {}
            for ad, alanlar_ in (('gecmis', {'state': 'completed', 'worktree': 5, 'final_result': ['y'], 'tasks': ['x']}),
                                 ('aktif', {'state': 'running', 'prompt': 5, 'harcama_alarmi': ['z']})):
                rid = store.accept('lab', f'bozuk {ad}')['run_id']
                yol = store.path(rid) / 'run.json'
                yol.write_text(json.dumps({**json.loads(yol.read_text()), **alanlar_}))
                bozuklar[ad] = rid
            (store.path(bozuklar['aktif']) / 'proje.json').write_text(json.dumps({'ozellikler': [{'id': 'f1', 'kosu': {'a': 1}, 'durum': 'takildi'}]}))
            _, kok = self.agac(home)
            gorevler = bul(kok, 'gorevler')
            aktif, gecmis = bul(gorevler, bozuklar['aktif']), bul(gorevler, 'gecmis', bozuklar['gecmis'])
            self.assertEqual([(n['ozet'], n['durum']) for n in (aktif, gecmis)], [('Okunamadı', 'hata')] * 2)
            self.assertTrue(aktif['aktif'] and aktif['kosu'] == bozuklar['aktif'], 'okunamayan çalışan görev yine iptal edilebilir')
            self.assertIn('"worktree": 5', arayuz_veri.dosya_oku(gecmis['dosya'])[2])
            self.assertNotEqual(bul(gorevler, iyi)['ozet'], 'Okunamadı')
            self.assertIn('Takılan parça · lab', [u['ad'] for u in bul(kok, 'panel', 'dikkat')['cocuk']])

    def test_missing_measurement_bad_cap_and_claude_alternatives(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            opus, sonnet = 'claude/claude-opus-x', 'claude/claude-sonnet-y'
            arayuz_veri.temizle()
            with patch('ekran.butce_gorunumu', return_value=({}, 0.0)), patch('yonlendirici.zincir', side_effect=AssertionError('ölçüm yok')), \
                 patch.dict(os.environ, {'CADRE_AYLIK_TAVAN_USD': 'abc'}):
                kok = arayuz_veri.agac(home)
            butce = bul(kok, 'butce')
            self.assertEqual((butce['ozet'], bul(butce, 'go')['ozet'], bul(butce, 'openrouter')['ozet'], bul(butce, 'zen')['ozet']),
                             ('Ölçüm yok', 'Aylık —', '—', '—'), 'ölçüm yokken sıfır gösterilmez')
            self.assertEqual((bul(butce, 'nakit')['ozet'], alanlar(bul(kok, 'ayarlar', 'nakit'))['Değer']), ('$0,00 / Geçersiz', 'Geçersiz'))
            self.assertEqual((bul(kok, 'ayarlar', 'dil')['eylem'], bul(kok, 'ajanlar', 'plan')['ozet']), ('dil', 'Ölçüm yok'))
            gorunum = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'), 'or': {'kalan': 5, 'kullanilabilir': 5},
                       'claude': {'pay': 2}}
            arayuz_veri.temizle()
            with patch('ekran.butce_gorunumu', return_value=(gorunum, 300)), patch('yonlendirici.zincir', side_effect=AssertionError('eski ölçüm')):
                eski = bul(arayuz_veri.agac(home), 'ajanlar', 'plan')
            self.assertEqual((eski['ozet'], alanlar(eski['cocuk'][0])['Neden']), ('Ölçüm eski', 'Bütçe ölçümü eski, sıra yeni ölçümle hesaplanır'),
                             'motor 90 sn\'den eski ölçümü kabul etmez, arayüz sıra uydurmaz')
            for kapali, zincir, beklenen in ((True, [GLM], ('1. ' + opus + ' | ' + sonnet, 'Elendi', 'Claude cadre içinde kapalı')),
                                             (False, [sonnet, GLM], ('1. ' + sonnet, 'İlk tercih', 'Zincirde, payı ve bağlamı uygun'))):
                arayuz_veri.temizle()
                with patch('ekran.butce_gorunumu', return_value=(gorunum, 30)), patch('yonlendirici.ROLLER', {'plan': [f'{opus}|{sonnet}', GLM]}), \
                     patch('yonlendirici.zincir', return_value=(zincir, [])), patch('yonlendirici.claude_kapali', return_value=kapali), \
                     patch('yonlendirici.durum_oku', return_value={}):
                    halka = bul(arayuz_veri.agac(home), 'ajanlar', 'plan')['cocuk'][0]
                self.assertEqual((halka['ad'], halka['ozet'], alanlar(halka)['Neden']), beklenen)
                self.assertEqual(alanlar(halka)['Seçenekler'], f'{opus} Claude payı 4 puan ve üstündeyse, değilse {sonnet}')

    def test_agents_section_runs_the_real_router_without_writing_or_network(self):
        import yonlendirici as Y
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / 'zincir').mkdir()

            def durum() -> dict:
                return {str(p.relative_to(home)): (p.stat().st_size, p.stat().st_mtime_ns) for p in home.rglob('*')}
            once = durum()
            gorunum = ({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'), 'or': {'kalan': 20, 'kullanilabilir': 20},
                        'go': {'kendi': {'monthly': 10}, 'kullanilabilir': 80}, 'claude': {'pay': 6}}, 30.0)
            arayuz_veri.temizle()
            arayuz_veri.SONDA.clear()
            with patch.object(Y, 'QE', home), patch.object(Y, 'KAYIT', home / 'zincir/yonlendirici.jsonl'), \
                 patch.object(Y, 'DURUM', home / 'zincir/saglayici-durum.json'), patch.object(Y, 'butce_oku', side_effect=AssertionError('ağ')):
                bolum = arayuz_veri.ajan_bolumu(home, gorunum)
                self.assertEqual(durum(), once, 'Ajanlar bölümü yönlendirici kaydına, durum dosyasına ya da bütçe önbelleğine yazmaz')
                Y.zincir('plan', b=gorunum[0], d={})
                self.assertTrue((home / 'zincir/yonlendirici.jsonl').is_file(), 'sınamanın yakalayacağı yazma gerçekten oluşur')
        self.assertEqual([c['id'] for c in bolum['cocuk']][:3], ['plan', 'impl', 'kalite'])


class TerminalDurumTests(unittest.TestCase):
    def kosu(self, store, project='personal', **values):
        rid = store.accept(project, 'Durum ağacı kabulü')['run_id']
        store.update(rid, state='running', **values)
        (store.path(rid) / 'plan.json').write_text(json.dumps({'gorevler': [
            {'id': 'g1', 'tanim': 'Başlamış iş'}, {'id': 'g2', 'tanim': 'Başlanmamış iş'}]}))
        for gid in ('g1', 'qa', 'kalite'):
            store.task_update(rid, gid, state='running')
            attempt = store.path(rid) / 'tasks' / gid / 'attempts/a1'
            attempt.mkdir(parents=True)
            (attempt / 'attempt.json').write_text(json.dumps({'role': 'kalite' if gid == 'kalite' else 'impl', 'model': GLM}))
            (attempt / 'stdout.jsonl').write_text('')
        return rid

    def dugumler(self, node):
        return [node] + [n for child in node['cocuk'] for n in self.dugumler(child)]

    def test_terminal_and_waiting_runs_distinguish_unfinished_unstarted_and_recorded_results(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = self.kosu(store)
            for lang in dil.DILLER:
                dil.kur(lang)
                for state in (*arayuz_veri.TERMINAL, 'accepted', 'queued', 'retryable'):
                    with self.subTest(lang=lang, state=state):
                        record = store.update(rid, state=state, final_result={})
                        before = {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
                        node = arayuz_veri.gorev_dugumu(home, record)
                        self.assertFalse([n['id'] for n in self.dugumler(node) if n['durum'] == 'simdi'])
                        self.assertEqual(node['su_an'], '')
                        label = dil.t('yarım kaldı') if state in arayuz_veri.TERMINAL else dil.t('bekliyor')
                        self.assertEqual(alanlar(bul(node, 'plan', 'g1'))[dil.t('Durum')], label)
                        self.assertEqual(bul(node, 'test')['ozet'], label)
                        self.assertEqual(bul(node, 'hakem')['ozet'], label)
                        if state in arayuz_veri.TERMINAL:
                            self.assertEqual(bul(node, 'plan', 'g2')['ozet'], dil.t('yapılmadı'))
                            self.assertIn('running', str(node['ayrinti']))
                        self.assertEqual(before, {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()})
                running = store.update(rid, state='running')
                node = arayuz_veri.gorev_dugumu(home, running)
                self.assertEqual([bul(node, *ids)['durum'] for ids in (('plan', 'g1'), ('test',), ('hakem',))], ['simdi'] * 3)
                for gid in ('qa', 'kalite'):
                    store.task_update(rid, gid, state=None)
                node = arayuz_veri.gorev_dugumu(home, store.read(rid))
                self.assertEqual([bul(node, gid)['durum'] for gid in ('test', 'hakem')], ['bekliyor'] * 2)
                self.assertFalse([n for n in bul(node, 'ajanlar')['cocuk'] if n['id'] in ('ajan-qa', 'ajan-kalite') and n['durum'] == 'simdi'],
                                 'taze çıktı ve görev varlığı tek başına aktif yürütme değildir')
                for result, marker, label in (('PASS', 'bitti', 'geçti'), ('FAIL', 'hata', 'kaldı'),
                                               ('ERROR', 'hata', 'hata'), ('CANCELLED', 'hata', 'yarım kaldı'),
                                               ('SKIPPED', '', 'atlanmış'), ('UNKNOWN', 'hata', 'durum bilinmiyor')):
                    record = store.update(rid, state='failed', final_result={'qa': {'status': result}, 'review': {'status': result}})
                    node = arayuz_veri.gorev_dugumu(home, record)
                    for gid in ('test', 'hakem'):
                        self.assertEqual((bul(node, gid)['durum'], bul(node, gid)['ozet']), (marker, dil.t(label)))
                for gid in ('qa', 'kalite'):
                    store.task_update(rid, gid, state='running')
            dil.kur('tr')

    def test_terminal_parent_never_projects_running_children_or_final_acceptance_as_active(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            parent = store.accept('personal', 'Parçalı ürün')['run_id']
            child = self.kosu(store, 'personal--pf2', proje_ust=parent)
            final = self.kosu(store, 'personal--pson', proje_ust=parent)
            project = {'durum': 'calisiyor', 'ozellikler': [
                {'id': 'f2', 'baslik': 'Yarım parça', 'durum': 'bekliyor', 'kosu': [child], 'riskli': True},
                {'id': 'f3', 'baslik': 'Başlanmamış parça', 'durum': 'bekliyor'}],
                'cocuklar': [{'ozellik': 'f2', 'kosu': child}, {'ozellik': 'son-kabul', 'kosu': final}]}
            (store.path(parent) / 'proje.json').write_text(json.dumps(project))
            for lang in dil.DILLER:
                dil.kur(lang)
                for state in arayuz_veri.TERMINAL:
                    with self.subTest(lang=lang, state=state):
                        record = store.update(parent, state=state, project_active_feature='f2')
                        before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
                        node = arayuz_veri.gorev_dugumu(home, record)
                        self.assertFalse([n['id'] for n in self.dugumler(node) if n['durum'] == 'simdi'])
                        self.assertEqual(node['su_an'], '')
                        self.assertEqual(bul(node, 'plan', 'f2')['ozet'], dil.t('yarım kaldı'))
                        self.assertEqual(bul(node, 'plan', 'f3')['ozet'], dil.t('yapılmadı'))
                        self.assertEqual(bul(node, 'son-kontrol')['ozet'], dil.t('yarım kaldı'))
                        self.assertIn(child, str(node['ayrinti']))
                        self.assertIn(final, str(node['ayrinti']))
                        self.assertIn('running', str(node['ayrinti']))
                        self.assertEqual(before, {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()})
                store.update(child, state='implemented', final_result={'status': 'UYGULANDI', 'qa': {'status': 'PASS'}})
                project['ozellikler'][0]['durum'] = 'uygulandi'
                (store.path(parent) / 'proje.json').write_text(json.dumps(project))
                for result, marker in (('FAIL', 'hata'), ('PASS', 'bitti')):
                    store.update(final, state='completed', final_result={'status': result})
                    node = arayuz_veri.gorev_dugumu(home, store.update(parent, state='completed'))
                    self.assertEqual(bul(node, 'plan', 'f2')['ozet'], dil.t('uygulandı'))
                    self.assertEqual(bul(node, 'son-kontrol')['durum'], marker)
                store.update(child, state='running', final_result={})
                store.update(final, state='running', final_result={})
                project['ozellikler'][0]['durum'] = 'bekliyor'
                (store.path(parent) / 'proje.json').write_text(json.dumps(project))
            project['ozellikler'][0]['kosu'] = ['missing-child']
            project['cocuklar'] = [{'ozellik': 'son-kabul', 'kosu': 'missing-final'}]
            (store.path(parent) / 'proje.json').write_text(json.dumps(project))
            node = arayuz_veri.gorev_dugumu(home, store.read(parent))
            self.assertEqual(bul(node, 'plan', 'f2')['ozet'], dil.t('durum bilinmiyor'))
            self.assertEqual(bul(node, 'son-kontrol')['ozet'], dil.t('durum bilinmiyor'))
            self.assertIn('missing-child', str(node['uyarilar']))
            self.assertIn('missing-final', str(node['uyarilar']))
            dil.kur('tr')


class GorevListesiTests(unittest.TestCase):
    def test_mixed_records_keep_titles_and_hide_test_runs_without_inflating_success(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            title = ('src/iPhone_API.py değişikliğini doğrula ' + 'uzun başlık ' * 15).strip()
            real = store.accept('test-dashboard', title)['run_id']
            store.update(real, state='running', created=time.time() - 120)
            passed = store.accept('personal', 'Tamamlanan ürün')['run_id']
            store.update(passed, state='completed', final_result={'status': 'PASS'})
            legacy = store.accept('canli-c01-20261010-070000', 'Test ürünü')['run_id']
            (store.path(legacy) / 'dispatch.log').write_text('2026-10-10 07:00:00 BITTI test-marker\n')
            marked = store.accept('laboratory', 'İşaretli test')['run_id']
            store.update(marked, test_run=True, state='completed', final_result={'status': 'PASS'})
            child = store.accept('laboratory-child', 'Testin alt işi')['run_id']
            store.update(child, proje_ust=marked, state='implemented')
            self.assertFalse(arayuz_veri.test_projesi({'project': 'test-dashboard'}))
            self.assertFalse(arayuz_veri.test_projesi({'project': 'personal', 'test_run': 'true'}))
            before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
            for lang in dil.DILLER:
                dil.kur(lang)
                arayuz_veri.temizle()
                hidden = arayuz_veri.agac(home, ajanlar=False)
                tasks = bul(hidden, 'gorevler')
                self.assertEqual(tasks['gizli_test'], 2)
                self.assertEqual({c['id'] for c in tasks['cocuk']}, {real, 'gecmis'})
                self.assertEqual(bul(tasks, real)['ad'], title)
                self.assertIn('test-dashboard', bul(tasks, real)['ozet'])
                self.assertIn('2 min ago' if lang == 'en' else '2 dk önce', bul(tasks, real)['ozet'])
                self.assertEqual([c['id'] for c in bul(tasks, 'gecmis')['cocuk']], [passed])
                self.assertEqual(alanlar(bul(hidden, 'panel', 'ozet'))[dil.t('Geçen')], '1')
                self.assertNotIn('test-marker', str(tasks['baglam']))
                dil.testleri_yaz(True, home)
                shown = arayuz_veri.agac(home, ajanlar=False)
                self.assertEqual(bul(shown, 'gorevler')['gizli_test'], 0)
                self.assertIn(legacy, {c['id'] for c in bul(shown, 'gorevler')['cocuk']})
                self.assertIn(marked, {c['id'] for c in bul(shown, 'gorevler', 'gecmis')['cocuk']})
                self.assertEqual(alanlar(bul(shown, 'panel', 'ozet'))[dil.t('Geçen')], '1')
                self.assertIn('test-marker', str(bul(shown, 'gorevler')['baglam']))
                dil.testleri_yaz(False, home)
            dil.kur('tr')
            self.assertEqual(before, {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()})
            (home / 'arayuz-testler').write_bytes(b'\xff')
            self.assertFalse(dil.testleri_oku(home))
            (home / 'arayuz-testler').write_text('true')
            self.assertFalse(dil.testleri_oku(home))

    def test_relative_age_cached_history_and_event_order_preserve_time_information(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            records = [store.accept('personal', f'Görev {i}') for i in range(7)]
            for i, k in enumerate(records):
                (store.path(k['run_id']) / 'dispatch.log').write_text(f'2026-10-10T07:00:0{i}Z plan kabul: {i}\n')
            oldest = store.path(records[0]['run_id']) / 'dispatch.log'
            oldest.write_text(oldest.read_text() + '2026-10-10T09:00:09+02:00 QA KAPISI PASS\n' * 2 + 'geçersiz\n2026-99-99 00:00:00 BITTI\n')
            events = arayuz_veri.son_olaylar(home, records)
            self.assertEqual(len(events), 6)
            self.assertIn('QA KAPISI PASS', events[0], 'eski koşunun yeni olayı kaybolmaz, saat dilimi sıralamada korunur')
            self.assertEqual(sum('QA KAPISI PASS' in e for e in events), 1)
            self.assertEqual(len(arayuz_veri.son_olaylar(home, records, 20)), 6)
            self.assertEqual(arayuz_veri.son_olaylar(home, records, 0), [])
            self.assertIn('2026-10-10T09:00:09+02:00', arayuz_veri.olaylar(home, records[0]['run_id'], mutlak=True)[-1])
            self.assertEqual(arayuz_veri.olaylar(home, 'missing'), [])
            self.assertEqual(arayuz_veri.olaylar(home, records[0]['run_id'], 0), [])
            k = store.update(records[0]['run_id'], state='failed', created=100, updated=160)
            for lang in dil.DILLER:
                dil.kur(lang)
                for bad in (None, 0, True, 'bad', float('nan'), float('inf'), 1001):
                    with patch('time.time', return_value=1000):
                        self.assertEqual(arayuz_veri.goreli_yas(bad), dil.t('zaman bilinmiyor'))
                for bad in (None, 0, True, 'bad', float('nan'), float('inf'), 1e30):
                    self.assertEqual(arayuz_veri.mutlak_zaman(bad), dil.t('zaman bilinmiyor'))
                for delta, key, n in ((30, 'az önce', 0), (120, '{n} dk önce', 2), (7200, '{n} sa önce', 2), (172800, '{n} gün önce', 2)):
                    with patch('time.time', return_value=1000000):
                        self.assertEqual(arayuz_veri.goreli_yas(1000000 - delta), dil.t(key, n=n))
                with patch('time.time', return_value=200):
                    first = arayuz_veri.gecmis_dugumu(home, k)['ozet']
                with patch('time.time', return_value=4000):
                    later = arayuz_veri.gecmis_dugumu(home, k)['ozet']
                self.assertNotEqual(first, later, 'önbellek göreli yaşı dondurmaz')
            dil.kur('tr')
            onceki = os.environ.get('TZ')
            try:
                outputs = []
                for zone in ('UTC', 'Europe/Istanbul'):
                    os.environ['TZ'] = zone
                    time.tzset()
                    outputs.append(alanlar(arayuz_veri.gorev_dugumu(home, k))['Başlangıç'])
                self.assertEqual(outputs, ['1970-01-01 00:01:40 +0000', '1970-01-01 02:01:40 +0200'])
            finally:
                if onceki is None:
                    os.environ.pop('TZ', None)
                else:
                    os.environ['TZ'] = onceki
                time.tzset()


class ProjeKayitTests(unittest.TestCase):
    def test_registers_this_folder_and_new_folders_safely(self):
        with tempfile.TemporaryDirectory() as t:
            kok = Path(t)
            home = kok / 'ev'
            from izin import approve, initialization_execute
            from kosu import RunStore
            with patch.dict(os.environ, GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@local',
                            GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@local', CADRE_RUN_ID=''):
                with self.assertRaises(proje_kayit.KayitIzni) as requested:
                    proje_kayit.kaydet(kok / 'Yeni Proje', olustur=True, home=home)
                self.assertFalse((kok / 'Yeni Proje' / '.git').exists())
                store = RunStore(home)
                first = approve(store, 'system', requested.exception.token)
                self.assertFalse((kok / 'Yeni Proje' / '.git').exists())
                approve(store, 'system', requested.exception.token, first['confirmation'])
                initialization_execute(store, requested.exception.token)
            self.assertEqual(proje_kayit.kaydet(kok / 'Yeni Proje', home=home), 'yeni-proje')
            self.assertEqual(git(kok / 'Yeni Proje', 'log', '--oneline').count('\n'), 0)
            self.assertEqual((home / 'projects/yeni-proje/base-branch').read_text().strip(), 'main')
            self.assertTrue((home / 'projects/yeni-proje/PROJECT.md').is_file() and (home / 'projects/yeni-proje/POLICY.md').is_file())
            self.assertEqual(proje_kayit.kaydet(kok / 'Yeni Proje', home=home), 'yeni-proje', 'aynı klasör iki kez kaydedilmez')
            (kok / 'dolu').mkdir()
            (kok / 'dolu/not.txt').write_text('x')
            with self.assertRaises(proje_kayit.KayitHatasi):
                proje_kayit.kaydet(kok / 'dolu', home=home)
            with self.assertRaises(proje_kayit.KayitHatasi):
                proje_kayit.kaydet(kok / 'dolu', olustur=True, home=home)
            bos_depo = kok / 'bos-depo'
            bos_depo.mkdir()
            git(bos_depo, 'init', '-q')
            with self.assertRaises(proje_kayit.KayitHatasi):
                proje_kayit.kaydet(bos_depo, home=home)
            with self.assertRaises(proje_kayit.KayitHatasi):
                proje_kayit.kaydet(bos_depo / 'ic', olustur=True, home=home)
            self.assertFalse((bos_depo / 'ic').exists(), 'başka deponun içinde yeni proje açılmaz')
            ikinci = kok / 'baska' / 'yeni-proje'
            (ikinci / 'alt').mkdir(parents=True)
            (ikinci / 'a.txt').write_text('a')
            git(ikinci, 'init', '-q', '-b', 'gelistirme')
            git(ikinci, 'add', '.')
            git(ikinci, 'commit', '-qm', 'ilk')
            self.assertEqual(proje_kayit.kaydet(ikinci / 'alt', home=home), 'yeni-proje-2')
            self.assertEqual(((home / 'projects/yeni-proje-2/repo-path').read_text().strip(),
                              (home / 'projects/yeni-proje-2/base-branch').read_text().strip()), (str(ikinci.resolve()), 'gelistirme'))


OKUNURLUK = r"""
import asyncio, io, json, os, sys, tempfile
from pathlib import Path
os.environ.pop('NO_COLOR', None)
os.environ['COLORTERM'] = 'truecolor'
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
from kosu import RunStore
from rich.console import Console
from rich.style import Style

def parlaklik(hex):
    rgb = [int(hex[i:i+2], 16) / 255 for i in (1, 3, 5)]
    rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * w for v, w in zip(rgb, (0.2126, 0.7152, 0.0722)))

home = Path(os.environ['CADRE_HOME'])
store = RunStore(home)
k = store.accept('kisisel-proje', 'src/iPhone_API.py kimliğini koru ' + 'uzun başlık ' * 12)
rid = k['run_id']
test_id = store.accept('canli-c01-20261010-070000', 'Kontrollü test görevi')['run_id']
store.update(rid, state='running', budget_spent_usd=1.25, budget_cap_usd=3,
             tasks={'plan': {'state': 'uretti'}, 'g1': {'state': 'running', 'model': 'openrouter/openai/gpt-6.1-sol'}})
(home / 'runs' / rid / 'plan.json').write_text(json.dumps({'gorevler': [
    {'id': 'g1', 'tanim': 'src/iPhone_API.py', 'dosyalar': ['src/iPhone_API.py'], 'model': 'openrouter/openai/gpt-6.1-sol'}]}))
before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
kontrast, ekranlar = {}, []
kanit = Path(os.environ['CADRE_UI_KANIT']) if os.environ.get('CADRE_UI_KANIT') else None
if kanit:
    kanit.mkdir(parents=True, exist_ok=True)

async def kos(lang, palet, boy):
    dil.yaz(lang)
    dil.renk_yaz(palet)
    arayuz_veri.temizle()
    app = arayuz.Cadre(home=home)
    async with app.run_test(size=boy) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press('2')
        await pilot.pause()
        assert app.secili['kosu'] == rid
        assert 'src/iPhone_API.py' in app.secili['ad']
        svg = app.export_screenshot()
        if kanit:
            (kanit / f'{lang}-{palet.replace(" ", "-")}-{boy[0]}x{boy[1]}.svg').write_text(svg)
        assert 'src/iPhone_API.py' in svg
        if boy[0] == 100:
            await pilot.press('shift+down', 'shift+down')
            await pilot.pause()
            svg = app.export_screenshot()
            if kanit:
                (kanit / f'{lang}-{palet.replace(" ", "-")}-{boy[0]}x{boy[1]}-scrolled.svg').write_text(svg)
        assert ('Status' if lang == 'en' else 'Durum') in svg, (lang, palet, boy)
        assert ('Running' if lang == 'en' else 'Çalışıyor') in svg
        assert arayuz.R['para'] in svg
        for kimlik in ('#liste', '#ayrinti', '#ipucu'):
            region = app.query_one(kimlik).region
            assert region.width > 0 and region.height > 0
            assert region.right <= boy[0] and region.bottom <= boy[1], (palet, boy, kimlik, region)
        console = Console(width=32, file=io.StringIO())
        dugum = arayuz_veri.d('src/iPhone_API.py', '$1.25 · 12 min · gpt-6.1-sol', durum='simdi')
        lines = console.render_lines(app.liste_r([dugum], 0, 6), console.options, pad=True)
        bg = Style.parse('on ' + arayuz.R['yuzey']).bgcolor
        for line in lines[1:4]:
            assert sum(seg.cell_length for seg in line) == 32
            assert all(seg.style and seg.style.bgcolor == bg for seg in line if seg.cell_length), line
        for durum, role in (('bitti', 'iyi'), ('simdi', 'vurgu'), ('hata', 'kotu'), ('vendor_state', 'soluk')):
            marker = app.isaret({'durum': durum})
            assert str(marker.style) == arayuz.R[role]
        assert dil.t('vendor_state') == 'vendor_state'
        assert dil.t('src/iPhone_API.py') == 'src/iPhone_API.py'
        assert dil.t('nerede: {h}', h='iPhone_API/Repo-X') == ('Where: ' if lang == 'en' else 'Nerede: ') + 'iPhone_API/Repo-X'
        assert dil.t('iptal edildi') == ('Cancelled' if lang == 'en' else 'İptal edildi')
        assert dil.t('o  oku').startswith('o  ')
        for text, role in (('$1.25', 'para'), ('$1,25', 'para'), ('openrouter/openai/gpt-6.1-sol', 'model'), ('12 min', 'zaman'), ('12 dk', 'zaman'), ('09:32:01', 'zaman')):
            rich = arayuz.yazi(text)
            assert rich.plain == text
            assert any(span.start == 0 and span.end == len(text) and span.style == arayuz.R[role] for span in rich.spans), (text, rich.spans)
        ekranlar.append([lang, palet, list(boy)])
        await pilot.press('5')
        while app.secili.get('id') != 'renk':
            await pilot.press('down')
        await pilot.press('enter')
        await app.workers.wait_for_complete()
        assert dil.renk_oku() == dil.RENKLER[(dil.RENKLER.index(palet) + 1) % len(dil.RENKLER)]
        if palet == dil.RENKLER[0]:
            while app.secili.get('id') != 'testler':
                await pilot.press('down')
            await pilot.press('enter')
            await app.workers.wait_for_complete()
            assert dil.testleri_oku(home)
            assert test_id in {c['id'] for c in app.bolum('gorevler')['cocuk']}
            await pilot.press('2')
            await pilot.pause()
            if kanit:
                (kanit / f'{lang}-{boy[0]}x{boy[1]}-tests.svg').write_text(app.export_screenshot())
            await pilot.press('5')
            while app.secili.get('id') != 'testler':
                await pilot.press('down')
            await pilot.press('enter')
            await app.workers.wait_for_complete()
            assert not dil.testleri_oku(home)
            assert app.bolum('gorevler')['gizli_test'] == 1

assert set(arayuz.PALETLER) == set(dil.RENKLER)
for ad, p in arayuz.PALETLER.items():
    kontrast[ad] = {}
    for bg in ('arka', 'yuzey'):
        for fg in ('yazi', 'soluk', 'vurgu', 'iyi', 'uyari', 'kotu', 'para', 'model', 'zaman'):
            a, b = sorted((parlaklik(p[fg]), parlaklik(p[bg])))
            ratio = (b + 0.05) / (a + 0.05)
            kontrast[ad][fg + '/' + bg] = round(ratio, 3)
            assert ratio >= (3 if fg == 'soluk' else 4.5), (ad, fg, bg, ratio)
for lang in dil.DILLER:
    for ad in dil.RENKLER:
        for boy in ((200, 50), (100, 30)):
            asyncio.run(kos(lang, ad, boy))
            arayuz.Cadre(home=home)
            assert dil.renk_oku() == arayuz.R['ad']
for f in (dil.dosya(), dil.renk_dosyasi()):
    f.write_bytes(b'\xff')
assert dil.oku() == 'tr' and dil.renk_oku() == 'Gruvbox'
os.environ['CADRE_RENK'] = 'unknown'
assert arayuz.palet_kur() == 'Gruvbox'
assert dil.renk_yaz('unknown') == 'Gruvbox'
dil.renk_yaz('Tokyo Night')
os.environ['CADRE_RENK'] = 'Dracula'
assert arayuz.palet_kur() == 'Dracula'
del os.environ['CADRE_RENK']
assert arayuz.Cadre(home=home) is not None and arayuz.R['ad'] == 'Tokyo Night'
assert before == {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
result = {'screens': ekranlar, 'contrast': kontrast, 'run_read_only': True}
if kanit:
    (kanit / 'okunurluk.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
"""


class PanelOzetTests(unittest.TestCase):
    def test_compact_panel_limits_rows_preserves_critical_attention_and_marks_unknown_budget(self):
        from aylik_butce import MonthlyLedger
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}, clear=True):
            home = Path(directory)
            store = RunStore(home)
            ledger = MonthlyLedger(home / 'zincir/aylik-butce.db', cap=65_000_000)
            ledger.reserve('openrouter', 2_000_000)
            for language in ('tr', 'en'):
                dil.kur(language)
                for count in (0, 1, 20):
                    for index in range(count):
                        rid = store.accept(f'lab{index}', 'Long title ' * 80)['run_id']
                        store.update(rid, state='running', harcama_alarmi={'ts': time.time(), 'neden': 'approval needed'})
                    arayuz_veri.temizle()
                    tree = arayuz_veri.agac(home, ajanlar=False)
                    panel = bul(tree, 'panel')
                    self.assertLessEqual(len(panel['ayrinti']), 12)
                    self.assertEqual(panel['kompakt'], True)
                    self.assertEqual([n['id'] for n in panel['cocuk']], ['su-an', 'ozet', 'dikkat', 'panel-butce'])
                    text = str(panel['ayrinti'])
                    if count:
                        self.assertIn(dil.t('onay bekliyor'), text)
                    self.assertIn('$2', text)
                    self.assertIn('No measurement' if language == 'en' else 'Ölçüm yok', text)
            (home / 'runs/broken').mkdir()
            (home / 'runs/broken/run.json').write_text('{')
            (home / 'zincir/aylik-butce.db').write_bytes(b'broken database')
            arayuz_veri.temizle()
            panel = bul(arayuz_veri.agac(home, ajanlar=False), 'panel')
            self.assertLessEqual(len(panel['ayrinti']), 12)
            self.assertIn(dil.t('Kalan bilinmiyor'), str(panel['ayrinti']))
            self.assertTrue(any(n['id'] == 'dikkat-kayit' for n in bul(panel, 'dikkat')['cocuk']))
            dil.kur('tr')


class EkonomiTests(unittest.TestCase):
    def fixture(self, home):
        from butce_kapisi import BudgetLedger
        store = RunStore(home)
        parent = store.accept('personal', 'Forty features')['run_id']
        child = store.accept('personal-child', 'Partial implementation')['run_id']
        orphan = store.accept('personal-child', 'Live child not yet linked')['run_id']
        unrelated = store.accept('other', 'Unrelated')['run_id']
        store.update(parent, state='failed', final_result={'status': 'FAIL'}, project_budget_cap_usd=20)
        store.update(child, state='implemented', proje_ust=parent)
        store.update(orphan, state='running', proje_ust=parent)
        project = {'durum': 'stopped', 'ozellikler': [
            {'id': f'f{i}', 'durum': 'uygulandi' if i < 3 else 'bekliyor', 'kosu': [child, child] if i == 0 else []}
            for i in range(40)], 'cocuklar': [{'kosu': child, 'ozellik': 'f0'}]}
        (store.path(parent) / 'proje.json').write_text(json.dumps(project))
        ledger = BudgetLedger(home / 'zincir/request-budget.db')
        snapshot = {'bugun': time.strftime('%Y-%m-%d'), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'zen': {'db': 'ok', 'kullanilabilir': 30, 'pay': 30}, 'or': {'db': 'ok', 'kullanilabilir': 30, 'pay': 30},
                    'go': {'db': 'ok', 'kullanilabilir': 100, 'pay': 100, 'birim': 'puan', 'pencere_acik': True},
                    'groq': {'db': 'ok', 'bugun_istek': 0}}

        def request(rid, attempt, actual, estimated=False, provider='openrouter'):
            ident = ledger.reserve(provider, 1 if provider == 'groq' else 1_000_000, snapshot,
                                   {'run_id': rid, 'task_id': 'g1', 'attempt_id': attempt, 'model': GLM if provider == 'opencode-go' else OR_SOL}, gozlem=True)
            if actual is not None:
                ledger.settle(ident, actual, ('hayalet:' if estimated else 'receipt:') + ident)
            return ident

        request(parent, 'p1', 100_000)
        request(parent, 'go', 100_000, provider='opencode-go')
        request(parent, 'free', 1, provider='groq')
        request(child, 'a1', 200_000)
        request(child, 'a2', 300_000, estimated=True)
        pending = request(orphan, 'live1', None)
        request(unrelated, 'other', 900_000)
        return store, parent, child, orphan, project, ledger, pending

    def test_project_child_attempt_totals_match_real_ledger_and_do_not_double_count_references(self):
        from proje import proje_maliyeti
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}, clear=True):
            home = Path(directory)
            store, parent, child, orphan, project, ledger, pending = self.fixture(home)
            before = {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
            for language in dil.DILLER:
                dil.kur(language)
                arayuz_veri.temizle()
                node = arayuz_veri.harcama_dugumu(home, store.read(parent), project)
                self.assertEqual(node['ekonomi'], {'spent': 700_000, 'held': 1_000_000, 'measured': 400_000, 'estimated': 300_000,
                                                   'cash': 600_000, 'equivalent': 100_000})
                self.assertEqual(set(n['id'] for n in node['cocuk']), {parent, child, orphan})
                self.assertEqual(bul(node, child)['ekonomi']['spent'], 500_000)
                self.assertEqual(bul(node, child, 'g1:a1')['ekonomi']['spent'], 200_000)
                self.assertEqual(bul(node, child, 'g1:a2')['ekonomi']['estimated'], 300_000)
                self.assertEqual(sum(n['ekonomi']['spent'] for n in node['cocuk']), node['ekonomi']['spent'])
                self.assertEqual(sum(n['ekonomi']['held'] for n in node['cocuk']), node['ekonomi']['held'])
                expected = proje_maliyeti(home, [parent, child])
                self.assertAlmostEqual(expected['olculen_usd'] + expected['tahmini_usd'], node['ekonomi']['spent'] / 1e6, places=6)
                self.assertEqual(expected['askida_usd'], node['ekonomi']['held'] / 1e6)
                self.assertIn(dil.t('Koşu özeti ile defter farklı. Bu görünüm defteri kullanır.'), str(node['ayrinti']))
                task = arayuz_veri.gorev_dugumu(home, store.read(parent))
                self.assertEqual(alanlar(task)[dil.t('Ara uygulama')], '3/40')
                self.assertFalse(any(kind == 'cubuk' and value[0] == dil.t('İlerleme') for kind, value in task['ayrinti']))
                self.assertIn(dil.t('Aşama göstergesi ürün tamamlanma yüzdesi değildir.'), str(task['ayrinti']))
            self.assertEqual(before, {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()})
            dil.kur('tr')

    def test_history_refreshes_after_child_and_receipt_changes_without_parent_update(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}, clear=True):
            home = Path(directory)
            store, parent, child, orphan, project, ledger, pending = self.fixture(home)
            record = store.read(parent)
            observer = sqlite3.connect(ledger.path)
            try:
                observer.execute('pragma journal_mode=WAL')
                observer.execute('begin')
                observer.execute('select actual from reservation where id=?', (pending,)).fetchone()
                first = arayuz_veri.gecmis_dugumu(home, record)
                ledger.settle(pending, 400_000, 'real:' + pending)
                self.assertIsNone(observer.execute('select actual from reservation where id=?', (pending,)).fetchone()[0])
                second = arayuz_veri.gecmis_dugumu(home, record)
                self.assertTrue(Path(str(ledger.path) + '-wal').exists())
            finally:
                observer.close()
            self.assertEqual(bul(first, 'harcama')['ekonomi']['spent'], 700_000)
            self.assertEqual(bul(second, 'harcama')['ekonomi']['spent'], 1_100_000)
            self.assertEqual(bul(second, 'harcama')['ekonomi']['held'], 0)
            store.update(child, state='failed')
            third = arayuz_veri.gecmis_dugumu(home, record)
            self.assertNotEqual(str(bul(second, 'plan', 'f0')), str(bul(third, 'plan', 'f0')))
            self.assertEqual(store.read(parent), record)

    def test_missing_corrupt_and_invalid_costs_are_unknown_and_valid_siblings_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            parent = store.accept('personal', 'Unknown cost')['run_id']
            child = store.accept('personal-child', 'Known cost')['run_id']
            store.update(child, proje_ust=parent, budget_spent_usd=0.25)
            record = store.read(parent)
            for invalid in (None, 'bad', -1, True, float('nan'), float('inf')):
                store.update(parent, budget_spent_usd=invalid)
                node = arayuz_veri.harcama_dugumu(home, store.read(parent), {})
                self.assertIsNone(node['ekonomi']['spent'])
                self.assertEqual(bul(node, child)['ekonomi']['spent'], 250_000)
                self.assertIn(dil.t('bilinmiyor'), node['ozet'])
            store.update(parent, budget_spent_usd=0)
            node = arayuz_veri.harcama_dugumu(home, store.read(parent), {})
            self.assertEqual(node['ekonomi']['spent'], 250_000)
            self.assertIsNone(node['ekonomi']['measured'])
            self.assertIsNone(node['ekonomi']['estimated'])
            linked_missing = {'cocuklar': [{'kosu': 'missing'}]}
            self.assertIsNone(arayuz_veri.harcama_dugumu(home, store.read(parent), linked_missing)['ekonomi']['spent'])
            db = home / 'zincir/request-budget.db'
            db.parent.mkdir(exist_ok=True)
            db.write_bytes(b'corrupt database')
            self.assertIsNone(arayuz_veri.harcama_dugumu(home, record, {})['ekonomi']['spent'])

    def test_summary_keeps_partial_open_failed_and_retry_costs_without_inflating_product_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}, clear=True):
            home = Path(directory)
            store = RunStore(home)
            for i, state in enumerate(('completed', 'failed', 'implemented', 'running')):
                rid = store.accept('personal', 'Same title')['run_id']
                store.update(rid, state=state, final_result={'status': 'PASS' if state == 'completed' else 'UYGULANDI'}, budget_spent_usd=0.1, budget_held_usd=0.2)
                for attempt in ('a1', 'a2'):
                    path = store.task_dir(rid, 'g1') / 'attempts' / attempt
                    path.mkdir(parents=True)
                    (path / 'attempt.json').write_text(json.dumps({'state': 'done'}))
            test = store.accept('lab', 'Test')['run_id']
            store.update(test, test_run=True, state='completed', final_result={'status': 'PASS'}, budget_spent_usd=10)
            child = store.accept('personal-child', 'Child')['run_id']
            store.update(child, proje_ust=rid, state='implemented', budget_spent_usd=0.3, budget_held_usd=0.4)
            for show_tests in (False, True):
                dil.testleri_yaz(show_tests, home)
                arayuz_veri.temizle()
                summary = bul(arayuz_veri.agac(home, ajanlar=False), 'panel', 'ozet')
                fields = alanlar(summary)
                self.assertEqual(fields['Biten'], '3')
                self.assertEqual(fields['Geçen'], '1')
                self.assertEqual(fields['Düşen'], '1')
                self.assertEqual(fields['Açık'], '1')
                self.assertEqual(fields['Başarı'], '1/3 · %33')
                self.assertEqual(fields['Harcama'], '$0,70')
                self.assertEqual(fields['Askıda'], '$1,20')


class AyarDuzenlemeTests(unittest.TestCase):
    def test_two_process_edit_race_has_one_winner_and_lowering_preserves_monthly_holds(self):
        import kosu
        from aylik_butce import MonthlyLedger, usd_micro
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}, clear=True):
            home = Path(directory)
            state = kosu.butce_ayari(home, 'aylik')
            worker = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import kosu
request=json.load(sys.stdin)
home=Path(request['home'])
(home / ('ready-'+request['value'])).touch()
limit=time.monotonic()+10
while not (home/'go').exists():
    if time.monotonic()>limit: raise RuntimeError('barrier timed out')
    time.sleep(.01)
try:
    kosu.butce_ayari_yaz(home,'aylik','',request['value'],request['state'])
    print('saved')
except ValueError:
    print('conflict')
'''
            children = []
            try:
                for value in ('10', '20'):
                    child = subprocess.Popen([sys.executable, '-c', worker, str(LIB)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True, env=dict(os.environ))
                    children.append(child)
                    child.stdin.write(json.dumps({'home': directory, 'value': value, 'state': state}))
                    child.stdin.close()
                limit = time.monotonic() + 10
                while len(list(home.glob('ready-*'))) < 2 and time.monotonic() < limit:
                    time.sleep(.01)
                self.assertEqual(len(list(home.glob('ready-*'))), 2)
                (home / 'go').touch()
                results = []
                for child in children:
                    child.wait(timeout=15)
                    self.assertEqual(child.returncode, 0, child.stderr.read())
                    results.append(child.stdout.read().strip())
                self.assertEqual(sorted(results), ['conflict', 'saved'])
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)
                    child.stdout.close()
                    child.stderr.close()
            self.assertEqual(usd_micro('0.000001'), 1)
            self.assertEqual(usd_micro('9007199254.740991'), 2**53 - 1)
            ledger = MonthlyLedger(home / 'zincir/aylik-butce.db')
            ledger.reserve('openrouter', 2_000_000)
            state = kosu.butce_ayari(home, 'aylik')
            kosu.butce_ayari_yaz(home, 'aylik', '', '1', state)
            command = [sys.executable, str(LIB / 'aylik_butce.py')]
            status = subprocess.run(command, input='{"action":"status"}', capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(status.stdout)['held_micro'], 2_000_000)
            self.assertEqual(json.loads(status.stdout)['remaining_micro'], 0)
            denied = subprocess.run(command, input='{"action":"reserve","provider":"opencode","maximum":1}', capture_output=True, text=True)
            self.assertEqual(denied.returncode, 75)
            self.assertIn('monthly paid budget exhausted', denied.stderr)
            self.assertEqual(MonthlyLedger(home / 'zincir/aylik-butce.db').status()['held_micro'], 2_000_000)

    def test_budget_edits_persist_and_real_engine_and_monthly_cli_read_the_same_caps(self):
        import kosu
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}, clear=True):
            home = Path(directory)
            (home / 'projects/lab').mkdir(parents=True)
            store = RunStore(home)
            old = store.accept('lab', 'existing')['run_id']
            for kind, project, value in [('kosu', 'lab', '0.125'), ('aylik', '', '12.345678')]:
                before = kosu.butce_ayari(home, kind, project)
                kosu.butce_ayari_yaz(home, kind, project, value, before)
                after = kosu.butce_ayari(home, kind, project)
                self.assertEqual(after['deger'], float(value))
                self.assertEqual(after['kaynak'], 'dosya')
                self.assertEqual(kosu.butce_ayari(home, kind, project), after)
            self.assertEqual(store.read(old)['budget_cap_usd'], 2)
            self.assertEqual(store.accept('lab', 'new')['budget_cap_usd'], 0.125)
            result = subprocess.run([sys.executable, str(LIB / 'aylik_butce.py')], input='{"action":"status"}',
                                    capture_output=True, text=True, env=dict(os.environ), check=True)
            self.assertEqual(json.loads(result.stdout)['cap_micro'], 12_345_678)
            self.assertEqual(arayuz_veri.nakit_durumu(home)['sinir'], 12.345678)
            with patch.dict(os.environ, {'CADRE_AYLIK_TAVAN_USD': '4', 'CADRE_KOSU_TAVAN_USD': '0'}):
                self.assertEqual(cap_micro(home), 4_000_000)
                self.assertEqual(store.accept('lab', 'zero')['budget_cap_usd'], 0)
                for kind, project in [('kosu', 'lab'), ('aylik', '')]:
                    state = kosu.butce_ayari(home, kind, project)
                    self.assertEqual(state['kaynak'], 'ortam')
                    with self.assertRaises(ValueError):
                        kosu.butce_ayari_yaz(home, kind, project, '5', state)

    def test_invalid_caps_stale_edits_and_write_failure_never_replace_the_previous_value(self):
        import kosu
        from aylik_butce import MonthlyBudgetDenied
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory}, clear=True):
            home = Path(directory)
            (home / 'projects/lab').mkdir(parents=True)
            for kind, project in [('kosu', 'lab'), ('aylik', '')]:
                state = kosu.butce_ayari(home, kind, project)
                kosu.butce_ayari_yaz(home, kind, project, '1', state)
                state = kosu.butce_ayari(home, kind, project)
                path = Path(state['yol'])
                original = path.read_bytes()
                for invalid in ('', 'NaN', 'sNaN', 'inf', '-1', '1e100000', '9007199254.740992', True):
                    with self.subTest(kind=kind, value=invalid), self.assertRaises(ValueError):
                        kosu.butce_ayari_yaz(home, kind, project, invalid, state)
                    self.assertEqual(path.read_bytes(), original)
                with patch.object(kosu.os, 'replace', side_effect=OSError('disk unavailable')), self.assertRaises(OSError):
                    kosu.butce_ayari_yaz(home, kind, project, '2', state)
                self.assertEqual(path.read_bytes(), original)
                kosu.butce_ayari_yaz(home, kind, project, '2', state)
                with self.assertRaises(ValueError):
                    kosu.butce_ayari_yaz(home, kind, project, '3', state)
                self.assertEqual(path.read_text().strip(), '2')
            store = RunStore(home)
            previous = sorted(str(p.relative_to(home)) for p in home.rglob('*'))
            with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': 'nan'}), self.assertRaises(ValueError):
                store.accept('lab', 'must not start')
            self.assertEqual(sorted(str(p.relative_to(home)) for p in home.rglob('*')), previous)
            with patch.dict(os.environ, {'CADRE_AYLIK_TAVAN_USD': 'nan'}), self.assertRaises(MonthlyBudgetDenied):
                cap_micro(home)
            monthly = home / 'zincir/aylik-tavan-usd'
            monthly.write_text('invalid')
            with self.assertRaises(MonthlyBudgetDenied):
                cap_micro(home)
            monthly.write_text('0')
            with self.assertRaises(MonthlyBudgetDenied):
                cap_micro(home)

    def test_symlinks_unknown_projects_and_environment_changes_are_rejected(self):
        import kosu
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory}, clear=True):
            home = Path(directory)
            (home / 'projects/lab').mkdir(parents=True)
            state = kosu.butce_ayari(home, 'kosu', 'lab')
            with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '7'}), self.assertRaises(ValueError):
                kosu.butce_ayari_yaz(home, 'kosu', 'lab', '8', state)
            target = home / 'protected'
            target.write_text('private fixture')
            path = home / 'projects/lab/kosu-tavan-usd'
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                kosu.butce_ayari(home, 'kosu', 'lab')
            self.assertEqual(target.read_text(), 'private fixture')
            for project in ('../outside', 'unknown'):
                with self.assertRaises(ValueError):
                    kosu.butce_ayari_yaz(home, 'kosu', project, '1', {})


class RolSunumuTests(unittest.TestCase):
    def test_four_visible_roles_preserve_real_engine_role_ids_and_unknown_roles(self):
        import yonlendirici as Y
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = store.accept('personal', 'Rol eşlemesi')['run_id']
            roles = ('impl', 'impl-hafif', 'birlestirme', 'kurtarici', 'gelecek-rol')
            for i, role in enumerate(roles):
                attempt = store.path(rid) / f'tasks/g{i}/attempts/a1'
                attempt.mkdir(parents=True)
                (attempt / 'attempt.json').write_text(json.dumps({'role': role}))
            before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
            chains = {role: list(models) for role, models in Y.ROLLER.items()}
            for language in ('tr', 'en'):
                with patch.object(dil, 'DIL', language):
                    menu = arayuz_veri.ajan_bolumu(home, ({}, 0))
                    self.assertEqual([n['id'] for n in menu['cocuk']], ['plan', 'impl', 'kalite', 'on-degerlendirme'])
                    self.assertEqual([n['ad'] for n in menu['cocuk']], [dil.t(n) for n in ('Planlayıcı', 'Geliştirici', 'Hakem', 'Ön değerlendirici')])
                    self.assertEqual(bul(menu, 'impl', 'impl-hafif')['ad'], dil.t('Hafif geliştirme'))
                    nodes = arayuz_veri.ajan_dugumleri(home, store.read(rid))
                    self.assertEqual([alanlar(n)[dil.t('Motor rolü')] for n in nodes], list(roles))
                    self.assertEqual([alanlar(n)[dil.t('Rol')] for n in nodes], [dil.t('Geliştirici')] * 4 + ['gelecek-rol'])
            self.assertEqual(chains, Y.ROLLER)
            self.assertEqual(before, {p: p.read_bytes() for p in home.rglob('*') if p.is_file()})


class KayitGorunumuTests(unittest.TestCase):
    def test_combined_trace_keeps_child_attempt_request_revision_duplicates_and_unknown_times_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            parent = store.accept('lab', 'combined trace')['run_id']
            child = store.accept('lab-child', 'child')['run_id']
            store.update(child, proje_ust=parent)
            root = store.path(parent)
            (root / 'proje.json').write_text(json.dumps({'cocuklar': [{'kosu': child}, {'kosu': child}, {'kosu': 'missing-child'}]}))
            (root / 'coordinator.log').write_text('2026-10-10T10:02:00Z coordinator waiting\nunknown parent line\n')
            (root / 'dispatch.log').write_text('2026-10-10T10:01:00+00:00 dispatch plan\n')
            attempt = store.path(child) / 'tasks/g1/attempts/a1'; attempt.mkdir(parents=True)
            (attempt / 'attempt.json').write_text(json.dumps({'attempt_id': 'a1', 'role': 'impl', 'model': 'test-model'}))
            event = {'timestamp': '2026-10-10T10:00:00Z', 'type': 'tool_use', 'request_id': 'request-1', 'revision': 'rev-2',
                     'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}, 'output': 'PASS'}}}
            future = {'timestamp': '2026-10-10 10:04:00', 'type': 'future-kind', 'data': {'value': 'retain'}}
            (attempt / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in (event, event, future)) + '\n{"type":')
            before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
            with patch('subprocess.run', side_effect=AssertionError('trace must not launch a process')):
                trace = arayuz_veri.olay_izi(home, parent)
                self.assertEqual(trace, arayuz_veri.olay_izi(home, parent))
                timeline = arayuz_veri.iz_metni(trace)
                spec = arayuz_veri.olaylar_dugumu(home, parent)['dosya']
                self.assertEqual(arayuz_veri.dosya_oku(spec)[2], timeline)
                self.assertEqual(json.loads(arayuz_veri.dosya_oku(spec, ham=True)[2]), trace)
            tool = next(e for e in trace['events'] if e['type'] == 'tool_use')
            self.assertEqual(tool['lines'], [1, 2])
            self.assertEqual((tool['run_id'], tool['parent_run_id'], tool['child_id'], tool['task_id'], tool['attempt_id'], tool['request_id'], tool['revision']),
                             (child, parent, child, 'g1', 'a1', 'request-1', 'rev-2'))
            self.assertEqual(sum(e['type'] == 'tool_use' for e in trace['events']), 1)
            known = [e['timestamp'] for e in trace['events'] if e['timestamp'] is not None]
            self.assertEqual(known, sorted(known))
            self.assertIsNone(next(e for e in trace['events'] if e['type'] == 'future-kind')['timestamp'])
            self.assertIn('unknown parent line', timeline)
            self.assertIn('future-kind', timeline)
            self.assertIn('{\\"type\\":', json.dumps(trace))
            self.assertTrue(any(i['issue'] == 'missing' and 'missing-child' in i['source'] for i in trace['issues']))
            self.assertEqual(before, {p: p.read_bytes() for p in home.rglob('*') if p.is_file()})

    def test_trace_cli_json_markdown_and_replay_preserve_redaction_without_accessing_replay_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            rid = store.accept('lab', 'trace export')['run_id']; root = store.path(rid)
            secret = 'SYNTHETIC_PRIVATE_VALUE'
            (root / 'dispatch.log').write_text('Authorization: Bearer ' + secret + '\n-----BEGIN PRIVATE KEY-----\n' + secret +
                                               '\n-----END PRIVATE KEY-----\nafter block\n')
            attempt = root / 'tasks/g1/attempts/a1'; attempt.mkdir(parents=True)
            (attempt / 'stdout.jsonl').write_text(json.dumps({'type': 'future', 'payload': {'password': secret}, 'text': '```` keep text'}) + '\n')
            env = {**os.environ, 'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'}
            command = ['bash', str(LIB.parent / 'bin/cadre.sh'), 'iz']
            before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
            result = subprocess.run([*command, rid], text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout)
            trace = json.loads(result.stdout)
            self.assertTrue(any(e['lines'] == [5] and e['data'] == 'after block' for e in trace['events']))
            self.assertIn('[REDACTED]', result.stdout)
            for form in ('json', 'markdown'):
                replay = subprocess.run([*command, '--replay', '--format', form], input=result.stdout, text=True,
                                        capture_output=True, env={**env, 'CADRE_HOME': str(home / 'absent-home')}, timeout=10)
                self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertNotIn(secret, replay.stdout)
                if form == 'json':
                    self.assertEqual(json.loads(replay.stdout), trace)
                else:
                    self.assertIn('`````text', replay.stdout)
            injected = json.loads(result.stdout)
            injected['events'][0]['data'] = {'password': secret}
            with patch('arayuz_veri._iz_kaynagi', side_effect=AssertionError('replay must not read sources')):
                self.assertNotIn(secret, arayuz_veri.iz_metni(arayuz_veri.iz_replay(json.dumps(injected))))
            invalid = json.loads(result.stdout); invalid['events'][0]['timestamp'] = float('nan')
            with self.assertRaises(ValueError):
                arayuz_veri.iz_replay(json.dumps(invalid))
            invalid['events'][0]['timestamp'] = 10**400
            with self.assertRaises(ValueError):
                arayuz_veri.iz_replay(json.dumps(invalid))
            invalid = json.loads(result.stdout); invalid['sources'][0]['mtime'] = 10**400
            with self.assertRaises(ValueError):
                arayuz_veri.iz_replay(json.dumps(invalid))
            with self.assertRaises(ValueError):
                arayuz_veri.iz_replay('{"schema":"unknown"}')
            self.assertFalse((home / 'absent-home').exists())
            self.assertEqual(before, {p: p.read_bytes() for p in home.rglob('*') if p.is_file()})

    def test_trace_unreadable_large_linked_and_changing_sources_stay_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            rid = store.accept('lab', 'negative trace')['run_id']; root = store.path(rid)
            outside = home / 'synthetic-private'; outside.write_text('SYNTHETIC_DO_NOT_READ')
            (root / 'dispatch.log').symlink_to(outside)
            (root / 'coordinator.log').write_bytes(b'x' * 2_000_001)
            (root / 'plan.json').write_text('{')
            trace = arayuz_veri.olay_izi(home, rid)
            self.assertNotIn('SYNTHETIC_DO_NOT_READ', json.dumps(trace))
            self.assertEqual(sum(i['issue'] == 'unreadable-or-limit' for i in trace['issues']), 2)
            self.assertTrue(any(i['issue'] == 'malformed-json' for i in trace['issues']))
            (root / 'dispatch.log').unlink(); os.mkfifo(root / 'dispatch.log')
            self.assertTrue(any('dispatch.log' in i['source'] for i in arayuz_veri.olay_izi(home, rid)['issues']))
            original = arayuz_veri._iz_kaynagi
            def changed(home, relative):
                raw, modified, _ = original(home, relative)
                return raw, modified, True
            with patch('arayuz_veri._iz_kaynagi', side_effect=changed):
                changed_trace = arayuz_veri.olay_izi(home, rid)
            self.assertTrue(any(i['issue'] == 'changed-during-read' for i in changed_trace['issues']))
            with self.assertRaises(ValueError):
                arayuz_veri.olay_izi(home, '../escape')

    def test_trace_event_limit_is_explicit_and_intermediate_symlink_swap_does_not_read_external_data(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            rid = store.accept('lab', 'bounded trace')['run_id']; root = store.path(rid)
            attempt = root / 'tasks/g1/attempts/a1'; attempt.mkdir(parents=True)
            log = attempt / 'stdout.jsonl'
            log.write_text('\n'.join(json.dumps({'type': 'future', 'index': n}) for n in range(5001)))
            trace = arayuz_veri.olay_izi(home, rid)
            self.assertEqual(len(trace['events']), 5000)
            self.assertTrue(any(i['issue'] == 'event-limit' for i in trace['issues']))
            outside = home / 'outside-trace'; (outside / 'g1/attempts/a1').mkdir(parents=True)
            (outside / 'g1/attempts/a1/stdout.jsonl').write_text('SYNTHETIC_DO_NOT_READ')
            log.write_text(json.dumps({'type': 'safe', 'timestamp': 10**400}) + '\n')
            self.assertIsNone(next(e for e in arayuz_veri.olay_izi(home, rid)['events'] if e['type'] == 'safe')['timestamp'])
            original_open = os.open
            swapped = False
            def swap(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == 'tasks' and not swapped:
                    swapped = True
                    (root / 'tasks').rename(root / 'parked-tasks')
                    (root / 'tasks').symlink_to(outside)
                return original_open(path, flags, *args, **kwargs)
            with patch('arayuz_veri.os.open', side_effect=swap):
                raw_trace = arayuz_veri.olay_izi(home, rid)
            self.assertTrue(swapped)
            self.assertNotIn('SYNTHETIC_DO_NOT_READ', json.dumps(raw_trace))
            self.assertTrue(any(i['issue'] == 'unreadable-or-limit' for i in raw_trace['issues']))

    def test_tail_windows_never_present_a_partial_secret_or_false_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stdout.jsonl'
            path.write_text('Authorization: Bearer ' + 'sentetik' * 50000 + '\n{"type":"gelecek"}\n')
            events = arayuz_veri.kayit_olaylari(path)
            self.assertEqual(len(events), 1)
            self.assertIn('stdout.jsonl@>', events[0][2])
            self.assertNotIn('sentetik', str(events))
            self.assertEqual(arayuz_veri.son_satirlar(path, 5), ['{"type": "gelecek"}'])

    def test_live_reader_keeps_budget_permission_and_terminal_states_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = store.accept('personal', 'İzleme sınırları')['run_id']
            for state in ('running', 'retryable', 'cancelled', 'failed', 'completed'):
                store.update(rid, state=state, reason='butce-bekleme', budget_cap_usd=0.01, budget_spent_usd=0.01,
                             harcama_alarmi={'ts': time.time(), 'neden': 'onay bekliyor'})
                before = {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
                with patch('subprocess.run', side_effect=AssertionError('izleyici süreç/ağ başlatamaz')):
                    node = arayuz_veri.izleme_verisi(home, rid)
                self.assertEqual(node['id'], rid)
                self.assertIn('butce-bekleme', str(node['ayrinti']))
                self.assertEqual(store.read(rid)['budget_cap_usd'], 0.01)
                self.assertEqual(before, {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()})
                self.assertIsNone(node['son_eylem_zamani'])
            path = store.path(rid) / 'run.json'
            path.write_text('{')
            with self.assertRaises(ValueError):
                arayuz_veri.izleme_verisi(home, rid)

    def test_common_events_raw_detail_redaction_and_read_only_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            attempt = Path(directory)
            token = 'sk-' + 'sentetik' * 4
            events = [
                {'type': 'reasoning', 'part': {'text': 'planı okuyorum'}},
                {'type': 'text', 'part': {'text': '{"sonuc": true}'}},
                {'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}, 'output': 'uzun-' * 100000 + token}}},
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pytest -q'}}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'araç tamamlandı'}]}},
                {'type': 'error', 'error': {'message': 'izin reddi', 'password': 'sentetik-parola'}},
                {'type': 'gelecek-olay', 'payload': {'Authorization': 'Bearer sentetik-yetki', 'env': {'OZEL': 'sentetik-ortam'}}},
                {'type': 'assistant', 'message': {'content': 'beklenmeyen içerik'}},
            ]
            source = attempt / 'stdout.jsonl'
            source.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events) + '\nbozuk password=sentetik-bozuk\n{"type":')
            before = source.read_bytes()
            for language in ('tr', 'en'):
                with patch.object(dil, 'DIL', language):
                    spec = {'tur': 'beyin', 'yol': str(attempt), 'baslik': 'Kayıt'}
                    _, _, summary = arayuz_veri.dosya_oku(spec)
                    _, _, raw = arayuz_veri.dosya_oku(spec, ham=True)
                    self.assertIn('pytest -q', summary)
                    self.assertEqual(summary.count('pytest -q'), 2)
                    self.assertIn('araç tamamlandı', summary)
                    self.assertIn('gelecek-olay', summary)
                    self.assertIn('stdout.jsonl:10', summary)
                    self.assertIn('\\"sonuc\\": true', raw)
                    self.assertIn('uzun-' * 100000, raw)
                    self.assertIn('{"type":', raw)
                    for secret in (token, 'sentetik-parola', 'sentetik-yetki', 'sentetik-ortam', 'sentetik-bozuk'):
                        self.assertNotIn(secret, summary + raw)
                    self.assertIn('[REDACTED]', raw)
                    self.assertIn('JSON', summary)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(raw, arayuz_veri.dosya_oku(spec, ham=True)[2])

    def test_log_sources_include_all_attempts_and_preserve_unknown_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = store.accept('lab', 'kayıt görünümü')['run_id']
            run = store.path(rid)
            (run / 'dispatch.log').write_text('2026-10-10 10:00:00 plan kabul\nbilinmeyen satır\n')
            (run / 'coordinator.log').write_text('koordinatör\n')
            for name in ('a1', 'a2'):
                attempt = run / 'tasks/g1/attempts' / name
                attempt.mkdir(parents=True)
                (attempt / 'stdout.jsonl').write_text('{"type":"yeni"}\n')
                (attempt / 'stderr.log').write_text('hata kaydı\n')
            outside = home / 'dis-kayit'
            outside.write_text('okunmamalı')
            (run / 'tasks/g1/attempts/a2/linked.log').symlink_to(outside)
            (run / 'tasks/g1/attempts/a3').symlink_to(home, target_is_directory=True)
            before = {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()}
            with patch('subprocess.run', side_effect=AssertionError('süreç/ağ çağrısı')):
                sources = arayuz_veri.log_kaynaklari(home, rid)
                node = arayuz_veri.olaylar_dugumu(home, rid)
                self.assertEqual(len(sources), 6)
                self.assertEqual(len(node['cocuk']), 6)
                self.assertEqual({s['yol'] for s in sources}, {c['dosya']['yol'] for c in node['cocuk']})
                self.assertIn('bilinmeyen satır', arayuz_veri.dosya_oku(node['dosya'])[2])
                for source in sources:
                    self.assertIn(Path(source['yol']).name, source['baslik'])
                    arayuz_veri.dosya_oku(source)
                    arayuz_veri.dosya_oku(source, ham=True)
            self.assertEqual(before, {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()})
            self.assertNotIn('okunmamalı', arayuz_veri.dosya_oku({'tur': 'log', 'yol': str(run / 'tasks/g1/attempts/a2/linked.log')})[2])

    def test_plain_json_and_incomplete_logs_mask_secrets_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.json'
            data = {'nested': {'api_key': 'sentetik-anahtar', 'DATABASE_URL': 'postgres://sentetik:sifre@localhost/db'}, 'task': 'g1'}
            data['tokens'] = {'input_tokens': 123, 'output_tokens': 45}
            data['text'] = json.dumps({'env': {'OZEL': 'sentetik-ortam'}})
            path.write_text(json.dumps(data))
            _, kind, text = arayuz_veri.dosya_oku({'tur': 'json', 'yol': str(path)})
            self.assertEqual(kind, 'json')
            self.assertIn('\n  "nested": {\n', text)
            self.assertEqual(json.loads(text)['task'], 'g1')
            self.assertEqual(json.loads(text)['tokens'], data['tokens'])
            self.assertNotIn('sentetik', text)
            path.write_text('Authorization: Bearer sentetik-yetki\nTOKEN=sentetik-token\n-----BEGIN PRIVATE KEY-----\nsentetik-ozel\n-----END PRIVATE KEY-----\nson satır')
            text = arayuz_veri.dosya_oku({'tur': 'log', 'yol': str(path)}, ham=True)[2]
            self.assertNotIn('sentetik', text)
            self.assertIn('son satır', text)
            path.unlink()
            self.assertIn(dil.t('Dosya okunamadı: {y}', y=path), arayuz_veri.dosya_oku({'tur': 'log', 'yol': str(path)})[2])


class ArayuzTests(unittest.TestCase):
    def test_task_defaults_editor_form_override_and_stale_submission_in_both_languages_and_sizes(self):
        script = r'''
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import arayuz,arayuz_veri,dil
from kosu import RunStore,gorev_varsayilanlari,gorev_varsayilanlari_yaz
from textual.widgets import Select,TextArea,Static
screens=[]
def capture(app,name):
    screens.append(name)
    if os.environ.get('CADRE_REQUEST_KANIT'):
        folder=Path(os.environ['CADRE_REQUEST_KANIT']); folder.mkdir(parents=True,exist_ok=True)
        (folder/(name+'.svg')).write_text(app.export_screenshot())
async def wait(app,pilot):
    await app.workers.wait_for_complete(); await pilot.pause(.1)
async def run(lang,size):
  with tempfile.TemporaryDirectory(prefix='cadre-defaults-ui-') as directory:
    home=Path(directory); repo=home/'repo'; repo.mkdir()
    (home/'projects/lab').mkdir(parents=True)
    (home/'projects/lab/repo-path').write_text(str(repo))
    os.environ.update(CADRE_HOME=str(home),CADRE_DIL=lang,CADRE_AG_YOK='1')
    dil.kur(lang); dil.EKSIK.clear(); arayuz_veri.temizle()
    store=RunStore(home); app=arayuz.Cadre(home=home)
    prefix=f'defaults-{lang}-{size[0]}'
    async with app.run_test(size=size) as pilot:
      await wait(app,pilot)
      app.git(['ayarlar']); app.secim=next(i for i,n in enumerate(app.ogeler) if n['id']=='tercih'); app.ciz()
      await pilot.press('enter'); assert isinstance(app.screen,arayuz.GorevVarsayilanlari)
      capture(app,prefix+'-editor')
      assert app.screen.query_one('#gv-kutu').region.bottom <= size[1]
      app.screen.query_one('#gv-preference',Select).value='economic'
      await pilot.press('escape')
      assert not (home/'zincir/gorev-varsayilanlari.json').exists() and not store.list()
      await pilot.press('enter')
      app.screen.query_one('#gv-preference',Select).value='strong'
      app.screen.query_one('#gv-difficulty',Select).value='hard'
      await pilot.press('ctrl+s'); await wait(app,pilot)
      assert gorev_varsayilanlari(home)['preference']=='strong' and not store.list()
      await pilot.press('enter'); editor=app.screen
      editor.query_one('#gv-preference',Select).value='economic'
      gorev_varsayilanlari_yaz(home,'balanced',None,gorev_varsayilanlari(home))
      await pilot.press('ctrl+s')
      assert app.screen is editor and gorev_varsayilanlari(home)['preference']=='balanced'
      capture(app,prefix+'-conflict')
      await pilot.press('escape')
      gorev_varsayilanlari_yaz(home,'strong','hard',gorev_varsayilanlari(home))
      app.push_screen(arayuz.YeniGorev([{'tur':'proje','ad':'lab','etiket':'lab'}])); await pilot.pause()
      await pilot.press('ctrl+s'); form=app.screen
      assert form.query_one('#yg-preference',Select).value=='strong'
      assert form.query_one('#yg-difficulty',Select).value=='hard'
      capture(app,prefix+'-inherited')
      form.query_one('#yg-metin',TextArea).text='Keep limits and defaults traceable'
      form.query_one('#yg-preference',Select).value='economic'
      form.query_one('#yg-difficulty',Select).value='easy'
      await pilot.press('ctrl+s'); assert form.adim==3 and not store.list()
      capture(app,prefix+'-summary')
      gorev_varsayilanlari_yaz(home,'balanced',None,gorev_varsayilanlari(home))
      await pilot.press('enter'); await wait(app,pilot)
      assert app.screen is form and not form.gonderiliyor and not store.list()
      capture(app,prefix+'-stale')
      await pilot.press('escape')
      app.push_screen(arayuz.YeniGorev([{'tur':'proje','ad':'lab','etiket':'lab'}])); await pilot.pause()
      await pilot.press('ctrl+s'); form=app.screen
      assert form.query_one('#yg-preference',Select).value=='balanced'
      assert form.query_one('#yg-difficulty',Select).value=='auto'
      form.query_one('#yg-metin',TextArea).text='Explicit override after reopening'
      form.query_one('#yg-preference',Select).value='economic'
      form.query_one('#yg-difficulty',Select).value='easy'
      await pilot.press('ctrl+s','enter'); await wait(app,pilot)
      saved=store.list()[0]; before=(store.path(saved['run_id'])/'run.json').read_bytes()
      assert saved['request']['preference']=='economic' and saved['request']['difficulty_override']=='easy'
      assert saved['request']['selection_defaults']['preference']=='balanced'
      assert saved['model_selection']['preference']=='economic' and saved['model_selection']['difficulty_override']=='easy'
      assert saved['model_selection']['difficulty_effective']=='easy'
      assert saved['budget_cap_usd']==2 and saved['budget_spent_usd']==0 and saved['tasks']=={}
      assert saved['reason']=='request-plan-gate' and saved['planning']['permissions']=='pending'
      gorev_varsayilanlari_yaz(home,'strong','hard',gorev_varsayilanlari(home))
      assert (store.path(saved['run_id'])/'run.json').read_bytes()==before
      app.kok_degistir(arayuz_veri.agac(home,ajanlar=False)); app.git(['gorevler',saved['run_id'],'secim'])
      await pilot.pause()
      assert dil.t('Etkin zorluk') in str(app.secili['ayrinti'])
      assert not dil.EKSIK, sorted(dil.EKSIK)
      capture(app,prefix+'-effective-selection')
      app.kok_degistir(arayuz_veri.agac(home,ajanlar=False)); app.git(['gorevler',saved['run_id'],'olaylar'])
      await pilot.press('o'); assert isinstance(app.screen,arayuz.Okuyucu)
      assert dil.t('Birleştirilmiş olay izi') in app.screen.baslik
      capture(app,prefix+'-trace')
      await pilot.press('r'); assert app.screen.ham and json.loads(app.screen.metin)['schema']=='cadre-trace-v1'
      capture(app,prefix+'-trace-json')
      await pilot.press('q')
      assert (store.path(saved['run_id'])/'run.json').read_bytes()==before
      assert not dil.EKSIK,sorted(dil.EKSIK)
for lang in ('tr','en'):
  for size in ((200,50),(100,30)):
    asyncio.run(run(lang,size))
print(json.dumps({'screens':len(screens),'cases':4}))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'screens': 32, 'cases': 4})

    def ui_python(self) -> Path:
        py = Path(os.environ.get('CADRE_TEST_ARAYUZ_PYTHON') or os.environ.get('CADRE_UI_PYTHON')
                  or Path.home() / '.local/share/cadre-ui-venv/bin/python')
        if not py.exists():
            self.skipTest('Textual ortami kurulu degil')
        return py

    def test_three_step_request_uses_the_real_cli_and_preserves_limits_in_both_languages_and_sizes(self):
        script = r'''
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import arayuz,arayuz_veri,dil
from kosu import RunStore
from textual.widgets import Input,Select,TextArea
evidence = os.environ.get('CADRE_REQUEST_KANIT')
screens = []
def capture(app,name):
    screens.append(name)
    if evidence:
        directory = Path(evidence); directory.mkdir(parents=True,exist_ok=True)
        (directory/(name+'.svg')).write_text(app.export_screenshot())
async def wait(pilot,app):
    await app.workers.wait_for_complete()
    await pilot.pause(.15)
async def run(lang,size):
  with tempfile.TemporaryDirectory(prefix='cadre-request-ui-') as directory:
    home = Path(directory); repo=home/'repo'; repo.mkdir()
    (home/'projects/lab').mkdir(parents=True)
    (home/'projects/lab/repo-path').write_text(str(repo))
    backlog=repo/'BACKLOG.md'; backlog.write_text('### [x] B001 Done\n### [ ] B002 Remaining\n')
    os.environ.update(CADRE_HOME=str(home),CADRE_AG_YOK='1',CADRE_DIL=lang)
    dil.kur(lang); dil.EKSIK.clear()
    store=RunStore(home); app=arayuz.Cadre(home=home)
    messages=[]; app.notify=lambda m,**kw:messages.append(str(m))
    prefix=f'{lang}-{size[0]}x{size[1]}'
    async with app.run_test(size=size) as pilot:
      await wait(pilot,app)
      app.push_screen(arayuz.YeniGorev([{'tur':'proje','ad':'lab','etiket':'lab'}]))
      await pilot.pause()
      await pilot.press('ctrl+s')
      app.screen.query_one('#yg-metin',TextArea).text='Cancel before submission'
      await pilot.press('ctrl+s')
      assert app.screen.adim==3 and not store.list()
      await pilot.press('escape')
      assert not isinstance(app.screen,arayuz.YeniGorev) and not store.list()
      app.push_screen(arayuz.YeniGorev([{'tur':'proje','ad':'lab','etiket':'lab'}]))
      await pilot.pause()
      screen=app.screen
      screen.query_one('#yg-mode',Select).value='backlog'
      await pilot.pause()
      screen.query_one('#yg-backlog',Input).value='BACKLOG.md'
      capture(app,prefix+'-source')
      await pilot.press('ctrl+s')
      screen.query_one('#yg-metin',TextArea).text='Preserve B001 and complete B002'
      screen.query_one('#yg-output',Input).value='Working output'
      screen.query_one('#yg-acceptance',Input).value='B002 tested; B001 preserved'
      screen.query_one('#yg-preference',Select).value='strong'
      screen.query_one('#yg-difficulty',Select).value='hard'
      screen.query_one('#yg-budget',Input).value='NaN'
      await pilot.press('ctrl+s')
      assert screen.adim==2 and not store.list(),messages
      capture(app,prefix+'-invalid')
      screen.query_one('#yg-budget',Input).value='0.25'
      screen.query_one('#yg-duration',Input).value='15'
      screen.query_one('#yg-duration',Input).focus()
      await pilot.pause()
      assert screen.query_one('#yg-duration').region.bottom <= size[1]
      capture(app,prefix+'-limits')
      await pilot.press('ctrl+s')
      assert screen.adim==3 and not store.list()
      await pilot.press('ctrl+b')
      assert screen.adim==2 and screen.query_one('#yg-budget',Input).value=='0.25'
      await pilot.press('ctrl+s')
      capture(app,prefix+'-summary')
      backlog.write_text('### [ ] B002 Source changed\n')
      await pilot.press('enter')
      await wait(pilot,app)
      assert app.screen is screen and not screen.gonderiliyor and not store.list(),messages
      capture(app,prefix+'-changed-source')
      await pilot.press('ctrl+b','ctrl+s')
      submitted=dict(screen.request)
      await pilot.press('enter','enter')
      await wait(pilot,app)
      assert app.screen is not screen
      records=store.list(); assert len(records)==1,records
      record=records[0]; rid=record['run_id']
      assert record['budget_cap_usd']==.25 and record['budget_spent_usd']==0 and record['tasks']=={}
      assert record['request']['duration_minutes']==15 and record['request']['preference']=='strong'
      assert record['request']['difficulty_override']=='hard' and record['planning']['difficulty_estimate'] is None
      assert record['request']['acceptance']=='B002 tested; B001 preserved'
      result=arayuz.subprocess.run(['bash',str(arayuz.MOTOR/'bin/cadre.sh'),'istek'],input=json.dumps(submitted),text=True,capture_output=True,env=dict(os.environ),timeout=10)
      assert result.returncode==0 and json.loads(result.stdout)['run_id']==rid,result.stderr
      assert len(store.list())==1
      app.kok_degistir(arayuz_veri.agac(home,ajanlar=False)); app.git(['gorevler',rid])
      await pilot.pause()
      capture(app,prefix+'-waiting')
      node=arayuz_veri.gorev_dugumu(home,record)
      assert node['durum']=='bekliyor' and node['asama']==dil.t('Plan kapısı bekleniyor')
      assert dil.t('{n} görev sürüyor',n=0) in app.bas_r().plain
      assert dil.t('{n} girdi plan bekliyor',n=1) in app.bas_r().plain
      assert node['girdi_bekliyor'] and not node.get('teslim')
      before={p:p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
      app.git(['gorevler',rid,'girdi'])
      await pilot.press('o')
      assert isinstance(app.screen,arayuz.Okuyucu)
      await pilot.press('q')
      assert before=={p:p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
      app.git(['gorevler'])
      app.secim=next(i for i,node in enumerate(app.ogeler) if node['id']==rid)
      app.ciz(); await pilot.pause()
      other=home/'other'; (other/'projects/lab').mkdir(parents=True)
      (other/'projects/lab/repo-path').write_text(str(repo))
      other_store=RunStore(other); other_record=other_store.request(submitted)
      os.environ['CADRE_HOME']=str(other)
      await pilot.press('x'); await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05); await pilot.press('enter')
      await wait(pilot,app)
      assert store.read(rid)['state']=='cancelled'
      assert other_store.read(other_record['run_id'])['state']=='accepted','Yanlış ev iptal edildi'
      assert not (home/'zincir').exists()
      assert sorted(p.name for p in repo.iterdir())==['BACKLOG.md']
      assert not dil.EKSIK,sorted(dil.EKSIK)
for lang in ('tr','en'):
  for size in ((200,50),(100,30)):
    asyncio.run(run(lang,size))
print(json.dumps({'screens':len(screens),'cases':4,'provider_calls':0}))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'screens': 24, 'cases': 4, 'provider_calls': 0})

    def test_project_economy_navigation_and_live_receipt_in_both_languages_and_sizes(self):
        script = r'''
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,str(Path(sys.argv[1]).parent/'tests'))
import arayuz,arayuz_veri,dil
from test_arayuz import EkonomiTests
def capture(app,name):
    if os.environ.get('CADRE_EKONOMI_KANIT'):
        (Path(os.environ['CADRE_EKONOMI_KANIT'])/(name+'.svg')).write_text(app.export_screenshot())
async def run(root):
    screens=0
    for language in dil.DILLER:
        dil.kur(language)
        for size in ((200,50),(100,30)):
            home=root/f'{language}-{size[0]}'
            home.mkdir()
            os.environ['CADRE_HOME']=str(home)
            store,parent,child,orphan,project,ledger,pending=EkonomiTests().fixture(home)
            arayuz_veri.temizle()
            dil.EKSIK.clear()
            before={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
            app=arayuz.Cadre(home=home)
            async with app.run_test(size=size) as pilot:
                for _ in range(100):
                    if app.yuklendi: break
                    await pilot.pause(.03)
                await app.workers.wait_for_complete()
                app.git(['gorevler','gecmis',parent])
                app.secim=next(i for i,n in enumerate(app.ogeler) if n['id']=='harcama')
                app.ciz()
                assert app.secili['ekonomi']['spent']==700_000
                assert app.secili['ekonomi']['estimated']==300_000
                await pilot.pause()
                prefix=f'{language}-{size[0]}x{size[1]}'
                capture(app,prefix+'-project')
                await pilot.press('enter')
                app.secim=next(i for i,n in enumerate(app.ogeler) if n['id']==child)
                app.ciz()
                assert app.secili['ekonomi']['spent']==500_000
                await pilot.pause()
                capture(app,prefix+'-child')
                await pilot.press('enter')
                app.secim=next(i for i,n in enumerate(app.ogeler) if n['id']=='g1:a2')
                app.ciz()
                assert app.secili['ekonomi']['spent']==app.secili['ekonomi']['estimated']==300_000
                await pilot.pause()
                capture(app,prefix+'-attempt')
                await pilot.press('q')
                assert app.zincir()[-1]['id']=='harcama'
                app.git(['gorevler','gecmis',parent,'harcama',orphan])
                assert app.secili['ekonomi']['held']==1_000_000
                assert before=={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
                original=store.read(parent)
                ledger.settle(pending,400_000,'receipt:'+pending)
                app.kok_degistir(arayuz_veri.agac(home,ajanlar=False))
                assert app.secili['ekonomi']['spent']==400_000 and app.secili['ekonomi']['held']==0
                assert store.read(parent)==original
                await pilot.pause()
                capture(app,prefix+'-settled')
                assert not dil.EKSIK,sorted(dil.EKSIK)
                screens+=4
    print(json.dumps({'screens':screens}))
with tempfile.TemporaryDirectory(prefix='cadre-economy-ui-') as directory:
    os.environ['CADRE_AG_YOK']='1'
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 16)

    def test_budget_editor_validation_preview_cancel_save_override_and_stale_source(self):
        script = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil, kosu
from aylik_butce import cap_micro
from textual.widgets import Input
async def run(home):
    screens = 0
    for language in ('tr', 'en'):
        dil.kur(language)
        for size in ((200, 50), (100, 30)):
            arayuz_veri.temizle()
            dil.EKSIK.clear()
            app = arayuz.Cadre(home=home)
            async with app.run_test(size=size) as pilot:
                for _ in range(100):
                    if app.yuklendi:
                        break
                    await pilot.pause(0.03)
                app.git(['ayarlar'])
                app.secim = next(i for i,n in enumerate(app.ogeler) if n.get('butce_ayar', {}).get('tur') == 'aylik')
                app.ciz()
                await pilot.press('enter')
                assert isinstance(app.screen, arayuz.ButceAyari)
                entry = app.screen.query_one(Input)
                state = app.screen.state
                path = Path(state['yol'])
                original = path.read_bytes() if path.exists() else None
                entry.value = 'NaN'
                await pilot.press('enter')
                assert not app.screen.onizleme
                assert (path.read_bytes() if path.exists() else None) == original
                assert str(app.screen.query_one('#ba-hata').render())
                if os.environ.get('CADRE_AYAR_KANIT'):
                    (Path(os.environ['CADRE_AYAR_KANIT']) / f'{language}-invalid-{size[0]}.svg').write_text(app.export_screenshot())
                entry.value = '70.125'
                await pilot.press('enter')
                assert app.screen.onizleme
                if os.environ.get('CADRE_AYAR_KANIT'):
                    (Path(os.environ['CADRE_AYAR_KANIT']) / f'{language}-review-{size[0]}.svg').write_text(app.export_screenshot())
                await pilot.press('escape')
                assert (path.read_bytes() if path.exists() else None) == original
                await pilot.press('enter')
                app.screen.query_one(Input).value = '70.125'
                await pilot.press('enter', 'ctrl+e')
                assert not app.screen.onizleme
                app.screen.query_one(Input).value = '71.125'
                await pilot.press('enter', 'enter')
                await app.workers.wait_for_complete()
                assert cap_micro(home) == 71_125_000 and path.read_text().strip() == '71.125'
                app.git(['ayarlar'])
                app.secim = next(i for i,n in enumerate(app.ogeler) if n.get('butce_ayar', {}).get('tur') == 'aylik')
                app.ciz()
                await pilot.press('enter')
                app.screen.query_one(Input).value = '12'
                await pilot.press('enter')
                path.write_text('9\n')
                await pilot.press('enter')
                assert isinstance(app.screen, arayuz.ButceAyari) and path.read_text().strip() == '9'
                await pilot.press('escape')
                os.environ['CADRE_AYLIK_TAVAN_USD'] = '7'
                app.yenile(True)
                await app.workers.wait_for_complete()
                node = arayuz_veri.butce_ayar_dugumu(home, 'aylik')
                assert node['butce_ayar']['deger'] == 7 and not node['eylem']
                assert cap_micro(home) == 7_000_000
                del os.environ['CADRE_AYLIK_TAVAN_USD']
                app.yenile(True)
                await app.workers.wait_for_complete()
                app.git(['ayarlar', 'ust-sinir'])
                assert app.secili['butce_ayar']['proje'] == 'lab'
                await pilot.press('enter')
                app.screen.query_one(Input).value = '0'
                await pilot.press('enter')
                if os.environ.get('CADRE_AYAR_KANIT'):
                    (Path(os.environ['CADRE_AYAR_KANIT']) / f'{language}-project-{size[0]}.svg').write_text(app.export_screenshot())
                await pilot.press('enter')
                assert kosu.kosu_tavani_usd(home, 'lab') == 0
                assert not dil.EKSIK, sorted(dil.EKSIK)
                screens += 3
    print(json.dumps({'screens': screens}))
with tempfile.TemporaryDirectory(prefix='cadre-settings-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_AG_YOK='1')
    (Path(directory) / 'projects/lab').mkdir(parents=True)
    (Path(directory) / 'projects/lab/repo-path').write_text(sys.argv[1])
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 12)

    def test_four_role_menu_and_lightweight_navigation_in_both_languages(self):
        script = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
async def run(home):
    count = 0
    for language in ('tr', 'en'):
        dil.kur(language)
        for size in ((200, 50), (100, 30)):
            arayuz_veri.temizle()
            app = arayuz.Cadre(home=home)
            async with app.run_test(size=size) as pilot:
                for _ in range(100):
                    if app.bolum('ajanlar')['cocuk']:
                        break
                    await pilot.pause(0.03)
                await pilot.press('4')
                assert [n['id'] for n in app.ogeler] == ['plan', 'impl', 'kalite', 'on-degerlendirme']
                for mode in ('roles', 'lightweight'):
                    if mode == 'lightweight':
                        await pilot.press('down', 'enter')
                        app.secim = next(i for i, n in enumerate(app.ogeler) if n['id'] == 'impl-hafif')
                        app.ciz()
                        await pilot.press('enter')
                        assert app.kap()['id'] == 'impl-hafif' and app.ogeler
                    if os.environ.get('CADRE_ROL_KANIT'):
                        (Path(os.environ['CADRE_ROL_KANIT']) / f'{language}-{mode}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                    count += 1
    print(json.dumps({'screens': count}))
with tempfile.TemporaryDirectory(prefix='cadre-role-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_AG_YOK='1')
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 8)

    def test_compact_dashboard_help_result_and_report_navigation_in_both_languages_and_sizes(self):
        script = r'''
import asyncio, io, json, os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
from kosu import RunStore
from aylik_butce import MonthlyLedger
from rich.console import Console
def capture(app, name):
    if os.environ.get('CADRE_PANEL_KANIT'):
        (Path(os.environ['CADRE_PANEL_KANIT']) / (name + '.svg')).write_text(app.export_screenshot())
async def run(root):
    screens = 0
    for language in ('tr', 'en'):
        dil.kur(language)
        for size in ((200, 50), (100, 30)):
            for mode in ('empty', 'many', 'stale', 'broken'):
                home = root / f'{language}-{size[0]}-{mode}'
                home.mkdir()
                os.environ['CADRE_HOME'] = str(home)
                store = RunStore(home)
                ledger = MonthlyLedger(home / 'zincir/aylik-butce.db', cap=65_000_000)
                ledger.reserve('openrouter', 2_000_000)
                for i in range(15 if mode == 'many' else 0):
                    record = store.accept(f'lab{i}', 'Long task title ' * 70)
                    store.update(record['run_id'], state='running', harcama_alarmi={'ts': time.time(), 'neden': 'approval needed'})
                if mode == 'stale':
                    cache = home / 'zincir/request-budget.json'
                    cache.write_text(json.dumps({'observed': time.time() - 600, 'budget': {'ts': '2026-01-01T12:00:00', 'bugun': '2026-01-01'}}))
                if mode == 'broken':
                    path = home / 'runs/broken'
                    path.mkdir(parents=True)
                    (path / 'run.json').write_text('{')
                    (home / 'zincir/aylik-butce.db').write_bytes(b'broken database')
                before = {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
                arayuz_veri.temizle()
                dil.EKSIK.clear()
                app = arayuz.Cadre(home=home)
                async with app.run_test(size=size) as pilot:
                    for _ in range(100):
                        if app.yuklendi:
                            break
                        await pilot.pause(0.03)
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    panel = app.bolum('panel')
                    console = Console(width=app.query_one('#ayrinti-metin').size.width, record=True, file=io.StringIO())
                    console.print(app.ayrinti_r(panel, True))
                    rendered = console.export_text()
                    assert len(rendered.splitlines()) <= 12, (language, size, mode, app.query_one('#ayrinti-metin').size.width, rendered, panel['ayrinti'])
                    assert app.query_one('#ayrinti').max_scroll_y == 0
                    assert not app.query_one('#alt').display and not app.query_one('#baglam').display
                    assert len(panel['ayrinti']) <= 12
                    capture(app, f'{language}-{mode}-{size[0]}x{size[1]}')
                    screens += 1
                    if mode == 'broken':
                        assert arayuz.t('Kalan bilinmiyor') in rendered, (language, size, rendered, panel['ayrinti'])
                        assert arayuz.t('Koşu kayıtları okunamadı')[:15] in rendered
                        app.git(['panel', 'dikkat'])
                        assert any(n['id'] == 'dikkat-kayit' for n in app.ogeler)
                    if mode == 'empty':
                        original = (list(app.yol), app.secim)
                        await pilot.press('question_mark')
                        assert isinstance(app.screen, arayuz.Okuyucu)
                        assert all(key in app.screen.metin for key,_ in arayuz.YARDIM_TUSLARI)
                        assert arayuz.t('Biten görev: Görevler › Geçmiş › görev › Sonuç / Teslim. Rapor işinde Rapor.') in app.screen.metin
                        capture(app, f'{language}-help-{size[0]}x{size[1]}')
                        await pilot.press('pagedown', 'pageup', 'escape')
                        assert (app.yol, app.secim) == original
                        assert {str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()} == before
                        wt = home / 'output'
                        wt.mkdir()
                        (wt / 'REPORT.md').write_text('# Report\n\nLocal fixture output.')
                        record = store.accept('report', 'TUR: rapor\nREPORT.md (yalnız bu dosyayı yaz)')
                        rid = record['run_id']
                        store.update(rid, state='completed', worktree=str(wt), final_result={'status':'PASS','qa':{'status':'PASS'},'review':{'status':'PASS'}})
                        arayuz_veri.temizle()
                        app.kok_degistir(arayuz_veri.agac(home, ajanlar=False))
                        app.git(['gorevler', 'gecmis', rid])
                        assert {'sonuc', 'teslim', 'rapor'}.issubset({n['id'] for n in app.ogeler})
                        app.secim = next(i for i,n in enumerate(app.ogeler) if n['id'] == 'sonuc')
                        app.ciz()
                        assert app.secili['ozet'] == 'PASS'
                        capture(app, f'{language}-result-{size[0]}x{size[1]}')
                        await pilot.press('o')
                        assert isinstance(app.screen, arayuz.Okuyucu) and 'final_result' in app.screen.metin
                        await pilot.press('q')
                        app.secim = next(i for i,n in enumerate(app.ogeler) if n['id'] == 'rapor')
                        app.ciz()
                        await pilot.press('o')
                        assert isinstance(app.screen, arayuz.Okuyucu) and 'Local fixture output.' in app.screen.metin
                        capture(app, f'{language}-report-{size[0]}x{size[1]}')
                        await pilot.press('q')
                        screens += 3
                    assert not dil.EKSIK, sorted(dil.EKSIK)
    print(json.dumps({'screens': screens}))
with tempfile.TemporaryDirectory(prefix='cadre-panel-ui-') as directory:
    os.environ['CADRE_AG_YOK'] = '1'
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 28)

    def test_readable_and_raw_log_navigation_in_both_languages_and_sizes(self):
        script = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
from kosu import RunStore
async def run(home):
    store = RunStore(home)
    rid = store.accept('personal', 'Kayıt okuma')['run_id']
    store.update(rid, state='completed')
    attempt = store.path(rid) / 'tasks/g1/attempts/a1'
    attempt.mkdir(parents=True)
    (attempt / 'attempt.json').write_text(json.dumps({'role': 'impl'}))
    (attempt / 'stdout.jsonl').write_text(json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}, 'output': 'satır\n' * 80}}}) + '\nbozuk\n')
    before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
    screens = []
    for language in ('tr', 'en'):
        dil.kur(language)
        for size in ((200, 50), (100, 30)):
            arayuz_veri.temizle()
            app = arayuz.Cadre(home=home)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.2)
                await app.workers.wait_for_complete()
                app.yol, app.secim = [1, 0], 0
                node = app.secili
                app.yol.append(app.ogeler.index(node))
                agents = next(i for i, c in enumerate(node['cocuk']) if c['id'] == 'ajanlar')
                app.secim = agents
                app.yol.append(agents)
                app.secim = 0
                app.ciz()
                await pilot.press('o')
                assert isinstance(app.screen, arayuz.Okuyucu)
                assert 'pytest -q' in app.screen.metin
                for mode in ('summary', 'raw'):
                    if mode == 'raw':
                        await pilot.press('r')
                        assert app.screen.ham
                        assert '"type": "tool_use"' in app.screen.metin
                    if os.environ.get('CADRE_KAYIT_KANIT'):
                        name = f'{language}-{mode}-{size[0]}x{size[1]}.svg'
                        (Path(os.environ['CADRE_KAYIT_KANIT']) / name).write_text(app.export_screenshot())
                        screens.append(name)
                await pilot.press('pagedown', 'r', 'q')
                assert app.secili['id'].startswith('ajan-')
                await pilot.press('o', 'escape')
                assert not isinstance(app.screen, arayuz.Okuyucu)
                assert store.read(rid)['state'] == 'completed'
    assert before == {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
    print(json.dumps({'screens': screens, 'read_only': True, 'cases': 4}))
with tempfile.TemporaryDirectory(prefix='cadre-log-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_AG_YOK='1')
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['cases'], 4)

    def test_live_monitor_refresh_selection_errors_reconnect_and_close(self):
        script = r'''
import asyncio, io, json, os, subprocess, sys, tempfile, time
from pathlib import Path
from rich.console import Console
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
from kosu import RunStore
async def wait(pilot, predicate, seconds=5):
    start = time.monotonic()
    while not predicate():
        assert time.monotonic() - start < seconds, 'live refresh deadline'
        await pilot.pause(0.03)
    return time.monotonic() - start
async def run(home):
    store = RunStore(home)
    rid = store.accept('personal', 'İlk koşu')['run_id']
    other = store.accept('personal', 'Sonraki koşu')['run_id']
    store.update(other, state='running')
    (store.path(rid) / 'plan.json').write_text(json.dumps({'gorevler': [{'id': 'g1', 'tanim': 'İlk iş', 'dosyalar': ['a.py'], 'kabul': 'pytest -q'}]}))
    log = store.path(rid) / 'dispatch.log'
    log.write_text('2026-10-10 10:00:00 plan kabul\n')
    readings = []
    for language in ('tr', 'en'):
        dil.kur(language)
        for size in ((200, 50), (100, 30)):
            store.update(rid, state='running', budget_spent_usd=0.1, budget_cap_usd=1)
            store.task_update(rid, 'g1', state='running')
            arayuz_veri.temizle()
            app = arayuz.Cadre(home=home)
            async with app.run_test(size=size) as pilot:
                await wait(pilot, lambda: app.yuklendi)
                app.yol = [1]
                app.secim = next(i for i, n in enumerate(app.ogeler) if n['id'] == rid)
                app.ciz()
                await pilot.press('i')
                screen = app.screen
                assert isinstance(screen, arayuz.CanliIzleme) and screen.run_id == rid
                await wait(pilot, lambda: bool(screen.veri))
                screen.secim = next(i for i, (p, n) in enumerate(screen.ogeler) if n.get('id') == 'plan')
                key = screen.ogeler[screen.secim][0]
                await pilot.press('o')
                assert isinstance(app.screen, arayuz.Okuyucu)
                await pilot.press('q')
                store.update(rid, budget_spent_usd=0.25)
                store.task_update(rid, 'g1', state='uretti')
                log.write_text(log.read_text() + '2026-10-10 10:00:01 gorev g1 uretti\n')
                seconds = await wait(pilot, lambda: any(t == 'alan' and v[0] == dil.t('Harcama') and arayuz_veri.para(0.25) in v[1] for t, v in screen.veri['ayrinti']))
                assert seconds <= 2.8, seconds
                assert screen.ogeler[screen.secim][0] == key
                assert screen.veri['kaynak_zamani'] and screen.veri['son_eylem_zamani']
                readings.append(seconds)
                source = store.path(rid) / 'run.json'
                saved = source.read_bytes()
                source.write_text('{')
                await wait(pilot, lambda: bool(screen.hata))
                assert screen.veri and screen.ogeler[screen.secim][0] == key
                console = Console(file=io.StringIO(), width=size[0], record=True)
                console.print(screen.query_one('#iz-durum').content)
                assert dil.t('Eski veri · okuma hatası: {e}', e=screen.hata) in console.export_text()
                if os.environ.get('CADRE_IZLEME_KANIT'):
                    (Path(os.environ['CADRE_IZLEME_KANIT']) / f'{language}-stale-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                source.write_bytes(saved)
                await wait(pilot, lambda: not screen.hata)
                if os.environ.get('CADRE_IZLEME_KANIT'):
                    (Path(os.environ['CADRE_IZLEME_KANIT']) / f'{language}-live-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                store.update(rid, state='completed', final_result={'status': 'PASS', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS'}})
                await wait(pilot, lambda: screen.veri.get('aktif') is False)
                assert {'plan', 'test', 'hakem', 'teslim'} <= {n['id'] for n in screen.veri['cocuk']}
                assert all(n['durum'] != 'simdi' for _, n in screen.ogeler)
                if os.environ.get('CADRE_IZLEME_KANIT'):
                    (Path(os.environ['CADRE_IZLEME_KANIT']) / f'{language}-terminal-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                store.update(rid, state='running')
                await wait(pilot, lambda: any(n.get('id') == rid for n in app.bolum('gorevler')['cocuk']))
                before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
                await pilot.press('down', 'up', 'pagedown', 'q')
                app.yol = [1]
                app.secim = next(i for i, n in enumerate(app.ogeler) if n['id'] == rid)
                app.ciz()
                await pilot.press('i')
                await wait(pilot, lambda: isinstance(app.screen, arayuz.CanliIzleme) and bool(app.screen.veri))
                assert app.screen.run_id == rid
                await pilot.press('escape')
                assert not isinstance(app.screen, arayuz.CanliIzleme)
                assert before == {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
                assert store.read(rid)['state'] == store.read(other)['state'] == 'running'
                if language == 'en' and size == (100, 30):
                    writer = "import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from kosu import RunStore; s=RunStore(Path(sys.argv[2])); [(s.update(sys.argv[3],budget_spent_usd=round(0.3+i/100,2)),time.sleep(0.2)) for i in range(5)]"
                    await pilot.press('i')
                    with subprocess.Popen([sys.executable, '-c', writer, sys.argv[1], str(home), rid]) as producer:
                        await pilot.press('q')
                        await wait(pilot, lambda: store.read(rid)['budget_spent_usd'] >= 0.3)
                        assert producer.poll() is None
                        assert await asyncio.to_thread(producer.wait, 5) == 0
                    assert store.read(rid)['budget_spent_usd'] == 0.34
                    assert store.read(rid)['state'] == 'running'
    result = {'cases': len(readings), 'refresh_seconds': readings, 'read_only': True, 'selected_run': rid, 'other_run': other}
    if os.environ.get('CADRE_IZLEME_KANIT'):
        (Path(os.environ['CADRE_IZLEME_KANIT']) / 'results.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
with tempfile.TemporaryDirectory(prefix='cadre-live-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_AG_YOK='1')
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['cases'], 4)

    def test_readability_contrast_full_selection_and_persisted_palettes_on_real_records(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as directory:
            r = subprocess.run([str(py), '-c', OKUNURLUK, str(LIB)], capture_output=True, text=True, timeout=300,
                               env={**os.environ, 'CADRE_HOME': directory, 'CADRE_AG_YOK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        result = json.loads(r.stdout.splitlines()[-1])
        self.assertEqual(len(result['screens']), 24)
        self.assertTrue(result['run_read_only'])

    def test_sample_mode_navigation_reader_new_task_language_and_live_updates(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([str(py), '-c', ORNEK, str(LIB)], capture_output=True, text=True, timeout=300, cwd=t,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_AG_YOK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        beklenen = ('gorevler|gorevler|Görevler>feedloom>Plan>f3 Kayıt tar>Plan|g2|False|5|Okuyucu|0:True|True:True|bu:yeni|3:yeni:yeniklasor|'
                    'en:True|[]|Everforest:Everforest:True|2')
        self.assertEqual(r.stdout.splitlines(), [beklenen, beklenen])

    def test_load_errors_confirm_cancel_and_empty_task_keep_the_app_alive(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([str(py), '-c', HATA, str(LIB), t], capture_output=True, text=True, timeout=300, cwd=t,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_AG_YOK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        sonuc = json.loads(r.stdout.splitlines()[-1])
        self.assertTrue(sonuc['ayakta'], 'okunamayan veri arayüzü kapatmaz')
        self.assertEqual([b for b in sonuc['bildirimler'] if b.startswith('Veriler okunamadı')], ['Veriler okunamadı: ValueError: bozuk kayıt'],
                         'aynı hata bir kez bildirilir')
        self.assertNotIn('Onay', sonuc['ekranlar'])
        self.assertEqual(sonuc['eylemler'], [], 'q ve Esc onaysız kapatır')
        self.assertEqual(sonuc['adim'], 2)
        self.assertIn('Görev metni boş. Önce görevi yaz.', sonuc['bildirimler'])


    def test_form_renders_code_blocks_plan_timeout_and_help_keys(self):
        """FIX-37/38/41: a `kod` block is drawn, plan generation is not tied to the panel's lifetime, the guide mentions the Approve/Space keys."""
        py = self.ui_python()
        script = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import arayuz, dil\n"
            "dil.kur('tr')\n"
            "parts = arayuz.form([('alan', ('A', 'B')), ('kod', 'KOD-METNI-123'), ('satir', 'sonra')])\n"
            "metin = ' '.join(getattr(part, 'plain', str(part)) for part in parts)\n"
            "rehber = arayuz.yardim_metni()\n"
            "import os, time, pathlib\n"
            "kayit = pathlib.Path(sys.argv[2]) / 'logs' / 'request-plan-r1.log'\n"
            "bas = time.monotonic()\n"
            "p = arayuz.arkada_baslat([sys.executable, '-c', 'import os, sys, time; time.sleep(1); print(os.getpgid(0)); sys.stderr.write(\"hata-satiri\")'], dict(os.environ), kayit)\n"
            "hizli = time.monotonic() - bas < 0.5\n"
            "p.wait(20)\n"
            "log = kayit.read_text()\n"
            "ayri = int(log.split()[0]) != os.getpgid(0)\n"
            "print(json.dumps(['KOD-METNI-123' in metin, hizli and ayri and 'hata-satiri' in log, 'Onay penceresi' in rehber, '/Space' in rehber]))\n")
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([str(py), '-c', script, str(LIB), t], capture_output=True, text=True, timeout=120,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_AG_YOK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(json.loads(r.stdout.splitlines()[-1]), [True, True, True, True],
                         'plan üretimi panelden ayrı oturumda, beklemeden başlar; çıktısı kayda yazılır')

    def test_new_task_modal_fits_small_screens_with_many_projects_and_long_text(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as calisma:
            r = subprocess.run([str(py), '-c', PENCERE, str(LIB), t], capture_output=True, text=True, timeout=300, cwd=calisma,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_AG_YOK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(json.loads(r.stdout.splitlines()[-1]), [[True, True, 3, True, True]] * 2,
                         'ipucu ve seçili proje görünür, uzun görev metni kısaltılır')


    def test_real_mode_registers_folders_sends_tasks_and_cancels_through_the_cli(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as repo:
            (Path(t) / 'projects/lab').mkdir(parents=True)
            (Path(t) / 'projects/lab/repo-path').write_text(repo)
            r = subprocess.run([str(py), '-c', GERCEK, str(LIB), t], capture_output=True, text=True, timeout=300,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_AG_YOK': '1'})
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            self.assertTrue((Path(t) / 'projects/yeni-proje/repo-path').is_file())
        sonuc = json.loads(r.stdout.splitlines()[-1])
        self.assertNotIn('okunuyor', sonuc['bas'])
        gorevler = [c for c in sonuc['cagrilar'] if c['komut'][-2] == 'gorev']
        self.assertEqual([(Path(c['komut'][1]).name, c['komut'][-1], 'repo' if c['cwd'] == repo else Path(c['cwd']).name, c['izle']) for c in gorevler],
                         [('cadre.sh', 'notlar.py ekle', 'repo', '0'), ('cadre.sh', 'ilk görev', 'yeni-proje', '0')])
        self.assertEqual(sonuc['onay'], 'Onay')
        self.assertEqual([c['komut'][-2:] for c in sonuc['cagrilar'] if 'cancel' in c['komut']], [['cancel', sonuc['aktif']]])
        self.assertEqual(sonuc['izlenen'], sonuc['aktif'],
                         'canlı izleme projenin son koşusunu değil seçili koşuyu açar')
        self.assertEqual([c for c in sonuc['cagrilar'] if 'izle' in c['komut']], [], 'panel izleme dış süreç açmaz')
        self.assertEqual(len(sonuc['tazeleme']), 1, 'eski bütçe ölçümü dakikada en çok bir kez motorun tazeleyicisine bırakılır, ağsız evde hiç')


UCTAN_UCA = """
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import arayuz, arayuz_veri, dil
dil.kur('tr')
home, bos = Path(sys.argv[2]), Path(sys.argv[3])
bildirimler = []
async def kos():
    app = arayuz.Cadre(ornek=False, home=home)
    app.notify = lambda m, **kw: bildirimler.append(str(m))
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause(0.5)
        await pilot.press('n')
        await pilot.pause()
        ilk = [s['tur'] for s in app.screen.secenekler]
        await pilot.press('up', 'enter', *'yeniproje', 'enter', *'merhaba', 'ctrl+s', 'enter')
        await app.workers.wait_for_complete()
        os.chdir(bos)
        await pilot.press('n')
        await pilot.pause()
        ikinci = [s['tur'] for s in app.screen.secenekler]
        await pilot.press('enter', *'ikinci', 'ctrl+s', 'enter')
        await app.workers.wait_for_complete()
        app.kok_degistir(arayuz_veri.agac(home, ajanlar=False))
        gorevler = app.bolum('gorevler')['cocuk']
        listede = sorted({str(c.get('proje')) for c in gorevler[:-1] + gorevler[-1]['cocuk']})
    return ilk, ikinci, listede
ilk, ikinci, listede = asyncio.run(kos())
print(json.dumps({'ilk': ilk, 'ikinci': ikinci, 'listede': listede, 'bildirimler': bildirimler}))
"""


class PlanOperationsUITests(unittest.TestCase):
    ui_python = ArayuzTests.ui_python


    def test_plan_operations_use_real_cli_and_read_only_history_in_both_languages_and_sizes(self):
        script = r"""
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import arayuz,arayuz_veri,dil
from kosu import RunStore,FileLock
from textual.widgets import TextArea,Static
screens=[]
def capture(app,name):
    screens.append(name)
    if os.environ.get('CADRE_PLAN_KANIT'):
        folder=Path(os.environ['CADRE_PLAN_KANIT']); folder.mkdir(parents=True,exist_ok=True)
        (folder/(name+'.svg')).write_text(app.export_screenshot())
async def wait(app,pilot):
    await app.workers.wait_for_complete(); await pilot.pause(.1)
def operation(app,rid):
    app.git(['gorevler',rid]); app.secim=next(i for i,c in enumerate(app.ogeler) if c.get('id')=='operasyon'); app.ciz()
async def run(lang,size):
  with tempfile.TemporaryDirectory(prefix='cadre-plan-ui-') as directory:
    home=Path(directory); os.environ.update(CADRE_HOME=str(home),CADRE_DIL=lang,CADRE_AG_YOK='1')
    dil.kur(lang); dil.EKSIK.clear(); arayuz_veri.temizle()
    store=RunStore(home); rid=store.accept('lab','README kapsaminda yazim duzelt')['run_id']
    plan={'is_id':'p1','gorevler':[{'id':'g1','tanim':'README yazimi','dosyalar':['README.md'],
          'kabul':'README icerigi korunur','model':'auto','zorluk':1,'paralel_grup':1,'tahmin_dk':1,'bagimli':[]}]}
    store.save_plan(rid,plan)
    log=home/'zincir/yonlendirici.jsonl'; log.parent.mkdir(exist_ok=True)
    row={'run_id':rid,'olay':'zincir','rol':'impl','ts':'2026-10-11T10:00:00','zincir':['opencode-go/glm-5.3'],
         'neden':['opencode-go/glm-5.3: OK','openrouter/model: cooldown api_key=synthetic_fixture_secret']}
    log.write_text(json.dumps(row)+'\n'+json.dumps(dict(row,run_id='unrelated'))+'\n'+json.dumps(dict(row,rol='incomplete')))
    app=arayuz.Cadre(home=home)
    async with app.run_test(size=size) as pilot:
      await wait(app,pilot)
      operation(app,rid); await pilot.pause()
      selected_operation=app.secili; detail=str(selected_operation['ayrinti'])
      assert 'cooldown' in detail and 'synthetic_fixture_secret' not in detail,detail
      assert 'incomplete' not in detail
      capture(app,f'{lang}-{size[0]}-decision')
      app.action_ac(); await pilot.pause()
      app.git(['gorevler',rid,'operasyon','plan-history'])
      before={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
      await pilot.press('o'); await pilot.pause()
      assert isinstance(app.screen,arayuz.Okuyucu)
      capture(app,f'{lang}-{size[0]}-history')
      await pilot.press('q'); await pilot.pause()
      assert before=={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
      app.git(['gorevler',rid,'operasyon','plan-change']); await pilot.press('enter'); await pilot.pause()
      assert isinstance(app.screen,arayuz.PlanDegisikligi)
      capture(app,f'{lang}-{size[0]}-change')
      await pilot.press('escape'); await pilot.pause()
      assert store.plan_view(rid)['changes']==[]
      app.git(['gorevler',rid,'operasyon','plan-change']); await pilot.press('enter','ctrl+s'); await pilot.pause()
      assert isinstance(app.screen,arayuz.PlanDegisikligi)
      assert str(app.screen.query_one('#plan-change-error',Static).render())
      app.screen.query_one('#plan-change-text',TextArea).load_text('Ayni kapsamda yontemi degistir')
      await pilot.press('ctrl+s'); await wait(app,pilot)
      assert store.plan_view(rid)['changes'][-1]['status']=='stop_requested'
      operation(app,rid); await pilot.pause(); capture(app,f'{lang}-{size[0]}-waiting')
      with FileLock(store.path(rid)/'.owner.lock'):
        app.git(['gorevler',rid,'operasyon','plan-stop']); await pilot.press('enter'); await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05); await pilot.press('enter'); await wait(app,pilot)
        assert store.plan_view(rid)['changes'][-1]['status']=='stop_requested'
      app.git(['gorevler',rid,'operasyon','plan-stop']); await pilot.press('enter'); await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05); await pilot.press('enter'); await wait(app,pilot)
      assert store.plan_view(rid)['changes'][-1]['status']=='stopped'
      operation(app,rid); await pilot.pause(); capture(app,f'{lang}-{size[0]}-stopped')
      app.git(['gorevler',rid,'operasyon','plan-withdraw']); await pilot.press('enter'); await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05); await pilot.press('enter'); await wait(app,pilot)
      assert store.plan_view(rid)['changes'][-1]['status']=='withdrawn'
      operation(app,rid); await pilot.pause(); capture(app,f'{lang}-{size[0]}-withdrawn')
      assert store.read(rid)['state']=='retryable'
      assert not dil.EKSIK,sorted(dil.EKSIK)
for lang in ('tr','en'):
  for size in ((200,50),(100,30)):
    asyncio.run(run(lang,size))
print(json.dumps({'screens':len(screens),'cases':4}))
"""
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'screens': 24, 'cases': 4})


class YeniGorevUctanUcaTests(e2e.E2EFixture):
    def yurutuculerin_cikisini_bekle(self):
        self.kill_guards()
        son = time.monotonic() + 10
        while time.monotonic() < son:
            r = subprocess.run(['pgrep', '-f', str(self.root)], capture_output=True, text=True, timeout=5)
            if r.returncode == 1:
                return
            self.assertEqual(r.returncode, 0, r.stderr)
            time.sleep(0.05)
        self.fail('İzole arayüz testinin yürütücüleri kapanmadı; geçici ev silinemez')

    def test_new_folder_and_registered_folder_use_the_real_request_without_implicit_git_actions(self):
        py = ArayuzTests.ui_python(self)
        self.addCleanup(self.yurutuculerin_cikisini_bekle)
        calisma, bos = Path(self.temp.name) / 'calisma', Path(self.temp.name) / 'bos-klasor'
        calisma.mkdir()
        bos.mkdir()
        (self.home / 'projects/bos-klasor').mkdir()
        (self.home / 'projects/bos-klasor/repo-path').write_text(str(bos))
        r = subprocess.run([str(py), '-c', UCTAN_UCA, str(self.root / 'lib'), str(self.home), str(bos)], capture_output=True, text=True, timeout=300,
                           cwd=calisma, env={**self.env, 'CADRE_AG_YOK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        sonuc = json.loads(r.stdout.splitlines()[-1])
        self.assertEqual((sonuc['ilk'][0], sonuc['ilk'][-1], sonuc['ikinci'][0]), ('bu', 'yeni', 'proje'))
        self.assertEqual([b for b in sonuc['bildirimler'] if 'kaydedildi' in b or 'SIRAYA' in b].__len__(), 2, sonuc['bildirimler'])
        kayitlar = {p.parent.name: Path(p.read_text().strip()) for p in (self.home / 'projects').glob('*/repo-path')}
        self.assertEqual(kayitlar['bos-klasor'].resolve(), bos.resolve())
        self.assertNotIn('yeniproje', kayitlar, 'Yeni proje kaydı örtülü izinle oluşturulmaz')
        self.assertFalse((calisma / 'yeniproje').exists())
        self.assertEqual(list(bos.iterdir()), [], 'Mevcut boş klasörde Git veya ilk commit oluşturulmaz')
        for proje, metin in (('yeniproje', 'merhaba'), ('bos-klasor', 'ikinci')):
            kosu = self.store.list(proje)
            self.assertEqual(len(kosu), 1, proje)
            self.assertIn(metin, Path(kosu[0]['prompt']).read_text())
            self.assertEqual(kosu[0]['planning']['status'], 'pending')
            self.assertEqual(kosu[0]['tasks'], {})
            self.assertEqual(kosu[0]['budget_spent_usd'], 0)
        self.assertTrue({'yeniproje', 'bos-klasor'} <= set(sonuc['listede']), sonuc['listede'])


if __name__ == '__main__':
    unittest.main()
