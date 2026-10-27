"""Sample data for the panel's design preview."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import arayuz_veri as V
import dil


def s(tr: str, en: str) -> str:
    return en if dil.DIL == 'en' else tr


def kosu_kimligi(ts: float) -> str:
    return time.strftime('%Y%m%d-%H%M%S', time.localtime(ts)) + '-' + uuid.uuid4().hex


def yaz(yol: Path, veri: Any) -> Path:
    yol.parent.mkdir(parents=True, exist_ok=True)
    gecici = yol.with_name(yol.name + '.yaziliyor')
    gecici.write_text(veri if isinstance(veri, str) else json.dumps(veri, ensure_ascii=False, indent=2))
    os.replace(gecici, yol)
    return yol


def ekle(yol: Path, satir: str) -> None:
    yol.parent.mkdir(parents=True, exist_ok=True)
    with yol.open('a') as f:
        f.write(satir + '\n')


def damga(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(['git', '-c', 'user.name=cadre', '-c', 'user.email=cadre@localhost', '-C', str(cwd), *args],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def arac(ts: float, ad: str, girdi: dict) -> dict:
    return {'type': 'tool_use', 'timestamp': int(ts * 1000), 'part': {'type': 'tool', 'tool': ad, 'state': {'status': 'completed', 'input': girdi}}}


def soz(ts: float, metin: str) -> dict:
    return {'type': 'text', 'timestamp': int(ts * 1000), 'part': {'type': 'text', 'text': metin}}


def deneme(kosu: Path, gid: str, rol: str, model: str, olaylar: list[dict]) -> Path:
    yol = kosu / 'tasks' / gid / 'attempts' / uuid.uuid4().hex
    yaz(yol / 'attempt.json', {'attempt_id': yol.name, 'run_id': kosu.name, 'task_id': gid, 'adapter': 'opencode', 'role': rol, 'model': model,
                               'session_id': 'ses_' + uuid.uuid4().hex[:24]})
    yaz(yol / 'stdout.jsonl', ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in olaylar))
    return yol


def guncelle(kosu: Path, **degerler: Any) -> dict:
    kayit = json.loads((kosu / 'run.json').read_text())
    kayit['updated'] = time.time()
    kayit.update(degerler)
    yaz(kosu / 'run.json', kayit)
    return kayit


def gorev_guncelle(kosu: Path, gid: str, **degerler: Any) -> None:
    kayit = json.loads((kosu / 'run.json').read_text())
    kayit.setdefault('tasks', {}).setdefault(gid, {}).update(degerler, updated=time.time())
    kayit['updated'] = time.time()
    yaz(kosu / 'run.json', kayit)


def olay(kosu: Path, ts: float, metin: str) -> None:
    ekle(kosu / 'dispatch.log', f'{damga(ts)} {metin}')


def qa_sonucu(kosu: Path, komut: str, durum: str, satirlar: list[str], sn: float) -> dict:
    cikti = yaz(kosu / 'tasks' / 'qa' / 'checks' / uuid.uuid4().hex / '1.log', '\n'.join(satirlar) + '\n')
    return {'status': durum, 'reason': 'command-result', 'command': komut,
            'attempts': [{'status': durum, 'exit_code': 0 if durum == 'PASS' else 1, 'seconds': sn, 'output': str(cikti)}]}


def repo_kur(home: Path, ad: str, dosyalar: dict[str, str]) -> tuple[Path, str]:
    repo = home / 'repos' / ad
    for yol, icerik in dosyalar.items():
        yaz(repo / yol, icerik)
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'ilk durum')
    proje = home / 'projects' / ad
    for dosya, icerik in (('repo-path', f'{repo}\n'), ('base-branch', 'main\n'), ('PROJECT.md', f'# {ad}\n'), ('POLICY.md', f'# POLICY: {ad}\n')):
        yaz(proje / dosya, icerik)
    return repo, git(repo, 'rev-parse', 'HEAD')


def calisma_agaci(home: Path, repo: Path, rid: str, degisiklik: dict[str, str], mesaj: str) -> Path:
    wt = home / 'worktrees' / repo.name / rid
    git(repo, 'worktree', 'add', '-q', '-b', f'cadre/{rid[:15]}', str(wt))
    for yol, icerik in degisiklik.items():
        yaz(wt / yol, icerik)
    git(wt, 'add', '.')
    git(wt, 'commit', '-qm', mesaj)
    return wt


def kosu_kur(home: Path, proje: str, ts: float, istem: str, **alanlar: Any) -> Path:
    rid = kosu_kimligi(ts)
    kosu = home / 'runs' / rid
    yaz(kosu / 'prompt.md', istem + '\n')
    kayit = {'run_id': rid, 'project': proje, 'state': 'running', 'created': ts, 'updated': ts, 'prompt': str(kosu / 'prompt.md'),
             'mode': 'uygulama', 'tasks': {}, 'budget_spent_usd': 0.0, 'budget_cap_usd': 2.0}
    kayit.update(alanlar)
    yaz(kosu / 'run.json', kayit)
    return kosu


def plan_yaz(kosu: Path, puan: int, gerekce: str, gorevler: list[dict]) -> None:
    yaz(kosu / 'plan.json', {'is_id': 'prompt', 'kapsam_puani': puan, 'kapsam_gerekce': gerekce, 'gorevler': gorevler,
                             'birlestirme_sirasi': [g['id'] for g in gorevler]})
    for g in gorevler:
        yaz(kosu / 'tasks' / g['id'] / 'prompt.md', '\n'.join([
            f"[QE-GOREV v1]\n{s('GOREV', 'TASK')} ({g['id']}): {g['tanim']}",
            f"{s('DOSYALAR (SADECE bunlara dokun)', 'FILES (touch ONLY these)')}: {', '.join(g['dosyalar'])}",
            f"{s('KABUL KRITERI', 'ACCEPTANCE')}: {g['kabul']}",
            f"{s('KAPSAM-DISI (ASLA dokunma)', 'OUT OF SCOPE (never touch)')}: {', '.join(g['kapsam_disi'])}"]) + '\n')


def alt_gorev(gid: str, tanim: str, dosyalar: list[str], kabul: str, bagimli: list[str], zorluk: int, grup: int, dk: int, usd: float) -> dict:
    return {'id': gid, 'tanim': tanim, 'dosyalar': dosyalar, 'kabul': kabul, 'kapsam_disi': ['tests/', 'BACKLOG.md'], 'bagimli': bagimli,
            'zorluk': zorluk, 'model': 'ds-pro', 'paralel_grup': grup, 'tahmin_dk': dk, 'tahmin_usd': usd}


def atama(kosu: Path, ts: float, proje: str, gid: str, model: str, durum: str, dk: int) -> None:
    ekle(kosu / 'atama-gecmisi.jsonl', json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts)), 'proje': proje, 'is_id': 'prompt',
                                                   'gorev_id': gid, 'fiili_model': model, 'baslangic_model': model, 'denenen_modeller': [model],
                                                   'son_model': model, 'durum': durum, 'sure_dk': dk}, ensure_ascii=False))


def harcama(gorev: str, model: str, saglayici: str, usd: float, rol: str = 'build') -> dict:
    return {'provider': saglayici, 'task': gorev, 'role': rol, 'model': model, 'n': 3, 'reserved_usd': usd, 'actual_usd': usd, 'pending': 0}


OR_DS = 'openrouter/deepseek/deepseek-v4-pro'
OR_SOL = 'openrouter/openai/gpt-6.1-sol'
GO_GLM = 'opencode-go/glm-5.3'
GO_QWEN = 'opencode-go/qwen3.8-max'
OR_DS_AD, OR_SOL_AD = OR_DS.split('/', 1)[1], OR_SOL.split('/', 1)[1]


def kodcu_olaylari(ts: float, dosya: str, test: str, dusunce: list[str]) -> list[dict]:
    return [arac(ts, 'read', {'filePath': dosya}), soz(ts + 40, dusunce[0]), arac(ts + 70, 'edit', {'filePath': dosya}),
            arac(ts + 110, 'bash', {'command': test}), soz(ts + 150, dusunce[1])]


def feedloom(home: Path, repo: Path, simdi: float) -> dict:
    t0 = simdi - 72 * 60
    jr = kosu_kur(home, 'feedloom', t0, s("BACKLOG.md'deki 'Cadre'ye hazır ilk 5 iş' bölümünü uygula.\nHer maddenin kabul komutu yanında yazıyor. "
                                         "tests/ altındaki mevcut testleri değiştirme. Push ve merge yapma.",
                                         "Implement the 'First 5 Cadre-ready items' section of BACKLOG.md.\nEach item lists its acceptance command. "
                                         "Do not change existing tests under tests/. Do not push or merge."),
                  budget_spent_usd=1.62, budget_cap_usd=10.0, project_active_feature='f3',
                  kapsam={'puan': 78, 'seviye': 'cok-zor', 'gerekce': s('Beş bağımsız madde, her biri kendi testiyle. Parçalı yürütülür.',
                                                                         'Five independent items, each with its own test. Run in parts.')},
                  tasks={'proje-liste': {'executors': [], 'updated': t0 + 120}})
    deneme(jr, 'proje-liste', 'plan', GO_GLM, [
        arac(t0 + 20, 'read', {'filePath': 'BACKLOG.md'}),
        soz(t0 + 50, s("BACKLOG.md'de 'Cadre'ye hazır ilk 5 iş' bölümünü buldum. Beş madde var, her birinin kabul komutu yazılı.",
                       "Found the 'First 5 Cadre-ready items' section in BACKLOG.md. Five items, each with an acceptance command.")),
        arac(t0 + 60, 'glob', {'pattern': 'tests/*.py'}), arac(t0 + 75, 'read', {'filePath': 'src/parser/tarih.py'}),
        soz(t0 + 100, s("f4 rapor özeti tarih alanını kullanıyor. f3'e bağladım.", 'f4 report summary uses the date field. Made it depend on f3.')),
        soz(t0 + 118, json.dumps({'ozellikler': ['f1', 'f2', 'f3', 'f4', 'f5']}))])
    ozellikler = [
        ('f1', s('Profil eşleme düzeltmesi', 'Profile matching fix'),
         s('Profil anahtar kelimeleri büyük küçük harften ve Türkçe karakterden bağımsız eşlensin.',
           'Profile keywords should match regardless of case and Turkish characters.'), 'tests/test_profil.py', [], 35, 0.6),
        ('f2', s('Kaynak tekrarı süzgeci', 'Duplicate source filter'),
         s('Aynı kayıt farklı kaynaklardan gelirse bir kez listelensin.', 'The same entry from different sources should be listed once.'),
         'tests/test_kaynak.py', [], 40, 0.5),
        ('f3', s('Kayıt tarihi ayrıştırma', 'Entry date parsing'),
         s("Göreli tarihler ('3 gün önce', 'dün') gerçek tarihe çevrilsin.", "Relative dates ('3 days ago', 'yesterday') should become real dates."),
         'tests/test_tarih.py', [], 55, 1.1),
        ('f4', s('Rapor özeti', 'Report summary'), s('Günlük raporun başına beş satırlık özet gelsin.', 'Add a five-line summary to the top of the daily report.'),
         'tests/test_rapor.py', ['f3'], 45, 0.7),
        ('f5', s('Dışa aktarma komutu', 'Export command'), s('feedloom disa-aktar --csv komutu eklensin.', 'Add a feedloom export --csv command.'),
         'tests/test_cli.py', [], 40, 0.6)]
    proje = {'kimlik': jr.name[:24], 'istek_ozeti': s("BACKLOG.md'deki ilk 5 Cadre işi, her biri kendi kabul komutuyla.",
                                                      'The first 5 Cadre items in BACKLOG.md, each with its own acceptance command.'),
             'ara_dal': f'cadre/proje-{jr.name[16:24]}', 'durum': 'calisiyor', 'harcama_usd': 1.62, 'tavan_usd': 10.0, 'cocuklar': [], 'ozellikler': []}
    for sira, (fid, baslik, aciklama, test, bagimli, puan, usd) in enumerate(ozellikler, 1):
        proje['ozellikler'].append({'id': fid, 'baslik': baslik, 'aciklama': aciklama, 'kabul': f'uv run pytest {test} -q', 'bagimli': bagimli,
                                    'oncelik': sira, 'durum': 'bekliyor', 'deneme': 0, 'kosu': [], 'qa_komut': f'uv run pytest {test} -q',
                                    'kapsam_puani': puan, 'tahmin_usd': usd, 'riskli': False})
    olay(jr, t0, s('kosu basliyor: proje kipi', 'run starting: project mode'))
    olay(jr, t0 + 120, s('proje plani kabul: 5 parca', 'project plan accepted: 5 parts'))
    alt_proje = f'feedloom--p{jr.name[16:24]}'
    adimlar = [
        ('f1', t0 + 180, [alt_gorev('g1', s('profil.py eşleme fonksiyonunu casefold ve normalize ile yeniden yaz',
                                            'Rewrite the profile.py matching function with casefold and normalize'),
                                    ['src/profil.py'], 'uv run pytest tests/test_profil.py -q', [], 2, 1, 10, 0.2),
                          alt_gorev('g2', s('Türkçe karakter eşleme testleri ekle', 'Add Turkish character matching tests'),
                                    ['tests/test_profil_tr.py'], 'uv run pytest tests/test_profil.py -q', ['g1'], 2, 2, 8, 0.15)],
         'tests/test_profil.py', ['14 passed in 0.42s']),
        ('f2', t0 + 1260, [alt_gorev('g1', s('ortak katmana kayıt parmak izi süzgeci ekle (başlık, kaynak, tarih)',
                                             'Add an entry fingerprint filter to the shared layer (title, source, date)'),
                                     ['src/kaynaklar/ortak.py'], 'uv run pytest tests/test_kaynak.py -q', [], 3, 1, 15, 0.3)],
         'tests/test_kaynak.py', ['9 passed in 0.31s'])]
    for fid, ts, gorevler, test, cikti in adimlar:
        c = kosu_kur(home, alt_proje, ts, f'{fid}: ' + next(o[2] for o in ozellikler if o[0] == fid),
                     proje_ust=jr.name, proje_ozellik=fid, plan_modeli=GO_GLM, budget_cap_usd=2.0,
                     kapsam={'puan': 35, 'seviye': 'orta', 'gerekce': s('Küçük ve testli bir değişiklik.', 'A small change with tests.')})
        plan_yaz(c, 35, s('Küçük ve testli bir değişiklik.', 'A small change with tests.'), gorevler)
        deneme(c, 'plan', 'plan', GO_GLM, [arac(ts + 10, 'read', {'filePath': 'BACKLOG.md'}),
                                           soz(ts + 40, s('Madde küçük. Alt görevlere böldüm.', 'Small item. Split into subtasks.')),
                                           soz(ts + 55, json.dumps({'gorevler': [g['id'] for g in gorevler]}))])
        dokum = [harcama('plan', 'glm-5.3', 'opencode-go', 0.05)]
        for g in gorevler:
            deneme(c, g['id'], 'impl', OR_DS, kodcu_olaylari(ts + 90, g['dosyalar'][0], g['kabul'], [
                s('Mevcut kodu okudum. Değişiklik tek fonksiyonda.', 'Read the existing code. The change is in one function.'),
                s('Testler yeşil. Kapsam dışı dosyaya dokunmadım.', 'Tests are green. Did not touch out-of-scope files.')]))
            gorev_guncelle(c, g['id'], state='uretti', attempt_count=1, last_executor=OR_DS)
            atama(c, ts + 400, c.name, g['id'], OR_DS, 'uretti', 7)
            dokum.append(harcama(g['id'], OR_DS_AD, 'openrouter', 0.21))
        qa = qa_sonucu(c, f'uv run pytest {test} -q', 'PASS', ['collected tests', *cikti], 4.2)
        gorev_guncelle(c, 'qa', qa_result=qa)
        gorev_guncelle(c, 'plan', executors=[])
        guncelle(c, state='implemented', plan_ready=True, budget_spent_usd=round(sum(x['actual_usd'] for x in dokum), 2), budget_breakdown=dokum,
                 updated=ts + 620, final_result={'status': 'UYGULANDI', 'qa': qa, 'review': {'status': 'SKIPPED', 'reason': 'proje-son-kabul-bekliyor'},
                                                 'reason': 'proje-son-kabul-bekliyor'})
        olay(c, ts + 30, s(f'plan kabul: {len(gorevler)} gorev', f'plan accepted: {len(gorevler)} tasks'))
        for g in gorevler:
            olay(c, ts + 400, f"gorev {g['id']} uretti")
        olay(c, ts + 600, s('QA KAPISI: PASS', 'QA GATE: PASS'))
        oz = next(o for o in proje['ozellikler'] if o['id'] == fid)
        oz.update(durum='gecti', deneme=1, kosu=[c.name])
        proje['cocuklar'].append({'ozellik': fid, 'kosu': c.name, 'durum': 'implemented', 'basari': True, 'neden': 'proje-son-kabul-bekliyor'})
        olay(jr, ts + 620, s(f'{fid} gecti', f'{fid} passed'))
    t3 = simdi - 26 * 60
    c3 = kosu_kur(home, alt_proje, t3, 'f3: ' + ozellikler[2][2], proje_ust=jr.name, proje_ozellik='f3', plan_modeli=OR_SOL,
                  plan_ready=True, budget_cap_usd=2.0, kapsam={'puan': 55, 'seviye': 'zor', 'gerekce': s(
                      'Ayrıştırıcı yeni, iki dosya ve yeni test gerekiyor.', 'The parser is new. Two files and a new test are needed.')})
    plan_yaz(c3, 55, s('Ayrıştırıcı yeni, iki dosya ve yeni test gerekiyor.', 'The parser is new. Two files and a new test are needed.'), [
        alt_gorev('g1', s('tarih.py içine göreli tarih ayrıştırıcısı yaz', 'Write a relative date parser in tarih.py'), ['src/parser/tarih.py'],
                  'uv run pytest tests/test_tarih.py -q', [], 3, 1, 15, 0.3),
        alt_gorev('g2', s('ortak.py içindeki eski ayrıştırma çağrısını yeni ayrıştırıcıya bağla', 'Wire the old parse call in ortak.py to the new parser'),
                  ['src/kaynaklar/ortak.py'], 'uv run pytest tests/test_tarih.py -q', ['g1'], 3, 2, 12, 0.25),
        alt_gorev('g3', s('göreli tarih testlerini yaz', 'Write the relative date tests'), ['tests/test_tarih.py'],
                  'uv run pytest tests/test_tarih.py -q', ['g1'], 2, 2, 10, 0.2)])
    deneme(c3, 'plan', 'plan', OR_SOL, [arac(t3 + 15, 'read', {'filePath': 'src/parser/tarih.py'}),
                                        soz(t3 + 50, s('Göreli tarih için bugünün tarihi gerekiyor. Saati sabitleyen bir yardımcı şart.',
                                                       "Relative dates need today's date. A helper that pins the clock is required.")),
                                        soz(t3 + 70, json.dumps({'gorevler': ['g1', 'g2', 'g3']}))])
    deneme(c3, 'g1', 'impl', OR_DS, kodcu_olaylari(t3 + 90, 'src/parser/tarih.py', 'uv run pytest tests/test_tarih.py -q', [
        s("'3 gün önce' ve 'dün' için ayrıştırıcı yazdım.", "Wrote the parser for '3 days ago' and 'yesterday'."),
        s('ISO ve 08 Eki biçimleri de çalışıyor.', 'ISO and 08 Oct formats work too.')]))
    g2 = deneme(c3, 'g2', 'impl', OR_DS, [arac(simdi - 420, 'read', {'filePath': 'src/kaynaklar/ortak.py'}),
                                          soz(simdi - 360, s("ortak.py'deki eski ayrıştırma ikinci kez çağrılıyor, onu kaldırmalıyım.",
                                                             'The old parser in ortak.py is called twice. I should remove it.')),
                                          arac(simdi - 300, 'edit', {'filePath': 'src/kaynaklar/ortak.py'}),
                                          arac(simdi - 200, 'bash', {'command': 'uv run pytest tests/test_tarih.py -q'}),
                                          soz(simdi - 150, s('Bir test saat dilimi yüzünden kırmızı. Europe/Istanbul sabitleyeceğim.',
                                                             'One test is red because of the time zone. I will pin Europe/Istanbul.'))])
    gorev_guncelle(c3, 'plan', executors=[])
    gorev_guncelle(c3, 'g1', state='uretti', attempt_count=1, last_executor=OR_DS)
    gorev_guncelle(c3, 'g2', state='running', attempt_count=1, last_executor=OR_DS)
    atama(c3, t3 + 600, c3.name, 'g1', OR_DS, 'uretti', 9)
    guncelle(c3, budget_spent_usd=0.31, budget_breakdown=[harcama('plan', OR_SOL_AD, 'openrouter', 0.08),
                                                           harcama('g1', OR_DS_AD, 'openrouter', 0.16),
                                                           harcama('g2', OR_DS_AD, 'openrouter', 0.07)])
    olay(c3, t3 + 60, s('plan kabul: 3 gorev', 'plan accepted: 3 tasks'))
    olay(c3, t3 + 600, 'gorev g1 uretti')
    olay(c3, t3 + 620, s('PARALEL: g2 ve g3 ayni grupta, g2 basladi', 'PARALLEL: g2 and g3 share a group, g2 started'))
    proje['ozellikler'][2].update(deneme=1, kosu=[c3.name])
    proje['son_cocuk'] = c3.name
    yaz(jr / 'proje.json', proje)
    olay(jr, t3, s('f3 basladi', 'f3 started'))
    guncelle(jr)
    return {'jr': jr, 'c3': c3, 'canli_g2': g2, 'alt_proje': alt_proje}


def notlar(home: Path, repo: Path, simdi: float) -> dict:
    t0 = simdi - 9 * 60
    nl = kosu_kur(home, 'notlar-lab', t0, s('notlar.py içine CSV dışa aktarma ekle ve 6 test yaz.', 'Add CSV export to notlar.py and write 6 tests.'),
                  plan_modeli=OR_SOL, plan_ready=True, budget_cap_usd=2.0, qa={'source': 'project', 'kind': 'project', 'command': 'make check'},
                  kapsam={'puan': 30, 'seviye': 'orta', 'gerekce': s('Yeni bir fonksiyon ve test dosyası.', 'A new function and a test file.')})
    gorevler = [alt_gorev('g1', s('notlar.py içine disa_aktar_csv fonksiyonunu ekle', 'Add disa_aktar_csv to notlar.py'), ['notlar.py'],
                          'make check', [], 2, 1, 8, 0.12),
                alt_gorev('g2', s('tests/test_csv.py içine 6 test yaz', 'Write 6 tests in tests/test_csv.py'), ['tests/test_csv.py'],
                          'make check', [], 2, 1, 6, 0.09)]
    plan_yaz(nl, 30, s('Yeni bir fonksiyon ve test dosyası.', 'A new function and a test file.'), gorevler)
    deneme(nl, 'plan', 'plan', OR_SOL, [arac(t0 + 10, 'read', {'filePath': 'notlar.py'}),
                                        soz(t0 + 30, s('İki bağımsız alt görev. Aynı anda yazılabilir.', 'Two independent subtasks. They can run in parallel.'))])
    for g in gorevler:
        deneme(nl, g['id'], 'impl', OR_DS, kodcu_olaylari(t0 + 60, g['dosyalar'][0], 'make check', [
            s('Dosyayı okudum. Yeni fonksiyonu sona ekliyorum.', 'Read the file. Appending the new function.'),
            s('make check yerelde yeşil.', 'make check is green locally.')]))
        gorev_guncelle(nl, g['id'], state='uretti', attempt_count=1, last_executor=OR_DS)
        atama(nl, t0 + 300, 'notlar-lab', g['id'], OR_DS, 'uretti', 4)
    gorev_guncelle(nl, 'plan', executors=[])
    gorev_guncelle(nl, 'qa', updated=simdi - 60)
    guncelle(nl, budget_spent_usd=0.29, budget_breakdown=[harcama('plan', OR_SOL_AD, 'openrouter', 0.08),
                                                           harcama('g1', OR_DS_AD, 'openrouter', 0.12),
                                                           harcama('g2', OR_DS_AD, 'openrouter', 0.09)])
    olay(nl, t0 + 40, s('plan kabul: 2 gorev', 'plan accepted: 2 tasks'))
    olay(nl, t0 + 300, 'gorev g1 uretti')
    olay(nl, t0 + 310, 'gorev g2 uretti')
    olay(nl, simdi - 60, s('QA KAPISI basladi: make check', 'QA GATE started: make check'))
    return {'nl': nl, 'nl_repo': repo, 'nl_taban': git(repo, 'rev-parse', 'HEAD')}


def gecmis(home: Path, repolar: dict, simdi: float) -> None:
    repo, taban = repolar['notlar-lab']
    h1 = kosu_kur(home, 'notlar-lab', simdi - 3 * 3600, s('Notlara etiket araması ekle (#etiket ile filtre).', 'Add tag search to notes (filter by #tag).'),
                  plan_modeli=OR_SOL, plan_ready=True, budget_cap_usd=2.0, source_sha=taban,
                  kapsam={'puan': 28, 'seviye': 'orta', 'gerekce': s('Tek fonksiyon ve test.', 'One function and a test.')})
    plan_yaz(h1, 28, s('Tek fonksiyon ve test.', 'One function and a test.'), [
        alt_gorev('g1', s('notlar.py içine etiket_ara fonksiyonunu ekle', 'Add etiket_ara to notlar.py'), ['notlar.py'], 'make check', [], 2, 1, 8, 0.1)])
    deneme(h1, 'g1', 'impl', OR_DS, kodcu_olaylari(simdi - 3 * 3600 + 60, 'notlar.py', 'make check', [
        s('Etiketler # ile başlıyor. Basit bir süzgeç yeterli.', 'Tags start with #. A simple filter is enough.'),
        s('make check yeşil.', 'make check is green.')]))
    deneme(h1, 'kalite', 'kalite', GO_QWEN, [arac(simdi - 2.6 * 3600, 'bash', {'command': f'git diff {taban[:7]}..HEAD --stat'}),
                                             soz(simdi - 2.6 * 3600 + 60, s('Değişiklik yalnız notlar.py ve yeni test. Kabul maddeleri karşılanıyor.',
                                                                            'Only notlar.py and a new test changed. Acceptance items are met.'))])
    wt = calisma_agaci(home, repo, h1.name, {'notlar.py': repo.joinpath('notlar.py').read_text() + '\n\ndef etiket_ara(notlar, etiket):\n'
                                                                                            '    return [n for n in notlar if etiket in n.split()]\n',
                                             'tests/test_etiket.py': 'from notlar import etiket_ara\n\n\ndef test_etiket():\n'
                                                                     "    assert etiket_ara(['a #x', 'b'], '#x') == ['a #x']\n"},
                        s('etiket araması', 'tag search'))
    qa = qa_sonucu(h1, 'make check', 'PASS', ['ruff check . ok', 'mypy ok', '33 passed in 1.12s'], 6.8)
    yaz(h1 / 'tasks' / 'kalite' / 'review-1.txt', s('Değişiklik iki dosyada. Test dosyaları dokunulmamış.\n\nKarar: GEÇER\n',
                                                    'Changes are in two files. Existing tests untouched.\n\nVerdict: PASS\n'))
    gorev_guncelle(h1, 'plan', executors=[])
    gorev_guncelle(h1, 'g1', state='uretti', attempt_count=1, last_executor=OR_DS)
    gorev_guncelle(h1, 'qa', qa_result=qa)
    gorev_guncelle(h1, 'kalite', executors=[])
    atama(h1, simdi - 2.8 * 3600, 'notlar-lab', 'g1', OR_DS, 'uretti', 6)
    guncelle(h1, state='completed', worktree=str(wt), budget_spent_usd=0.34, updated=simdi - 2.5 * 3600, final_result={
        'status': 'PASS', 'qa': qa, 'review': {'status': 'PASS', 'verdict': 'GECER', 'model': GO_QWEN, 'independent': True, 'writers': ['deepseek-v4-pro'],
                                               'excluded_writers': [OR_DS], 'repairs': 0, 'repair_limit': 1, 'acceptance': [
                                                   {'id': 'original_request', 'status': 'PASS', 'evidence': s(
                                                       "git diff yalnız notlar.py ve tests/test_etiket.py dosyalarını değiştirdi. etiket_ara '#x' ile doğru süzüyor.",
                                                       "git diff changed only notlar.py and tests/test_etiket.py. etiket_ara filters '#x' correctly.")},
                                                   {'id': 'g1', 'status': 'PASS', 'evidence': s('make check 33 test yeşil.', 'make check 33 tests green.')}]}})
    olay(h1, simdi - 3 * 3600 + 30, s('plan kabul: 1 gorev', 'plan accepted: 1 task'))
    olay(h1, simdi - 2.6 * 3600, s('HAKEM: GECER (qwen3.8-max, bagimsiz)', 'REVIEWER: PASS (qwen3.8-max, independent)'))
    olay(h1, simdi - 2.5 * 3600, s('KABUL SONUCU: PASS · BITTI', 'ACCEPTANCE RESULT: PASS · DONE'))
    jr_repo, jr_taban = repolar['feedloom']
    h2 = kosu_kur(home, 'feedloom', simdi - 6 * 3600, s('feedloom mimari özet raporu yaz (modül haritası, riskler, öneriler).',
                                                         'Write a feedloom architecture summary report (module map, risks, suggestions).'),
                  mode='rapor', plan_modeli=OR_SOL, source_sha=jr_taban, budget_cap_usd=2.0,
                  rapor={'yol': 'docs/reviews/mimari-ozet.md', 'bolumler': [s('Özet', 'Summary'), s('Modül haritası', 'Module map'),
                                                                            s('Riskler', 'Risks'), s('Öneriler', 'Suggestions')]})
    wt2 = calisma_agaci(home, jr_repo, h2.name, {'docs/reviews/mimari-ozet.md': s(
        '# Mimari özet\n\n## Özet\nKaynak adaptörleri ortak katmandan geçiyor.\n\n## Modül haritası\n- src/kaynaklar\n- src/parser\n\n'
        '## Riskler\n- Tarih ayrıştırma her kaynakta ayrı.\n\n## Öneriler\n- Ortak ayrıştırıcı.\n',
        '# Architecture summary\n\n## Summary\nSource adapters pass through a shared layer.\n\n## Module map\n- src/kaynaklar\n- src/parser\n\n'
        '## Risks\n- Date parsing differs per source.\n\n## Suggestions\n- A shared parser.\n')}, s('mimari özet raporu', 'architecture summary report'))
    guncelle(h2, state='completed', worktree=str(wt2), budget_spent_usd=0.83, updated=simdi - 5.4 * 3600, teslim_merge=damga(simdi - 5 * 3600),
             final_result={'status': 'PASS', 'qa': {'status': 'PASS', 'command': s('rapor denetimi', 'report check')},
                           'review': {'status': 'PASS', 'verdict': 'GECER', 'model': GO_QWEN, 'independent': True}})
    b_repo, b_taban = repolar['demo-lab']
    h3 = kosu_kur(home, 'demo-lab', simdi - 5 * 3600, s('Fiyat günlüğünde saat dilimini Europe/Istanbul yap.', 'Use Europe/Istanbul in the price log.'),
                  plan_modeli=OR_SOL, plan_ready=True, source_sha=b_taban, budget_cap_usd=2.0, qa={'command': 'pytest -q'})
    plan_yaz(h3, 25, s('Tek dosya.', 'One file.'), [alt_gorev('g1', s('fiyat.py saat dilimi düzeltmesi', 'fiyat.py time zone fix'), ['fiyat.py'],
                                                              'pytest -q', [], 2, 1, 6, 0.08)])
    deneme(h3, 'g1', 'impl', OR_DS, kodcu_olaylari(simdi - 5 * 3600 + 60, 'fiyat.py', 'pytest -q', [
        s('zoneinfo ile düzeltiyorum.', 'Fixing it with zoneinfo.'), s('İki test hâlâ kırmızı.', 'Two tests are still red.')]))
    qa3 = qa_sonucu(h3, 'pytest -q', 'FAIL', ['FAILED tests/test_fiyat.py::test_gece_yarisi', 'FAILED tests/test_fiyat.py::test_yaz_saati',
                                              '2 failed, 11 passed in 0.9s'], 3.1)
    gorev_guncelle(h3, 'g1', state='uretti', attempt_count=2, last_executor=OR_DS)
    gorev_guncelle(h3, 'qa', qa_result=qa3)
    gorev_guncelle(h3, 'duzeltme1', state='basarisiz', attempt_count=1, last_executor=OR_DS, reason=s('test hâlâ kırmızı', 'test still red'))
    deneme(h3, 'duzeltme1', 'impl', OR_DS, [soz(simdi - 4.6 * 3600, s('Yaz saati geçişi ayrıca ele alınmalı.', 'DST transition must be handled.'))])
    guncelle(h3, state='failed', reason=s('qa-kirmizi: 2 test kaldı', 'qa-red: 2 tests failed'), budget_spent_usd=0.21, updated=simdi - 4.5 * 3600,
             final_result={'status': 'FAIL', 'qa': qa3})
    olay(h3, simdi - 4.6 * 3600, s('QA KAPISI: FAIL (2 test)', 'QA GATE: FAIL (2 tests)'))
    h4 = kosu_kur(home, 'notlar-lab', simdi - 26 * 3600, s('README dosyasına kurulum adımlarını ekle.', 'Add setup steps to README.'),
                  plan_modeli=GO_GLM, plan_ready=True, budget_cap_usd=1.0)
    guncelle(h4, state='completed', budget_spent_usd=0.12, updated=simdi - 25.8 * 3600, teslim_push=damga(simdi - 25 * 3600),
             final_result={'status': 'PASS', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS', 'verdict': 'GECER', 'model': GO_QWEN}})
    h5 = kosu_kur(home, 'feedloom', simdi - 28 * 3600, s('Tüm kaynakları yeniden tara ve kırık olanları listele.', 'Rescan all sources and list broken ones.'),
                  budget_cap_usd=2.0)
    guncelle(h5, state='cancelled', reason=s('kullanıcı iptal etti', 'cancelled by user'), updated=simdi - 27.9 * 3600)


def defterler(home: Path, simdi: float) -> None:
    sema = ('create table reservation(id text primary key, provider text not null, period text not null, created real not null, '
            'reserved integer not null, actual integer, receipt text unique, metadata text not null)')
    (home / 'zincir').mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / 'zincir' / 'request-budget.db')) as con, con:
        con.execute(sema)
        satirlar = [('openrouter', OR_DS_AD, 'g2', 21000, 600), ('openrouter', OR_DS_AD, 'g1', 34000, 1500),
                    ('openrouter', OR_SOL_AD, 'plan', 80000, 1700), ('openrouter', OR_DS_AD, 'g1', 47000, 2400),
                    ('openrouter', OR_DS_AD, 'g2', 29000, 3300), ('opencode-go', 'qwen3.8-max', 'kalite', 120000, 9300),
                    ('opencode-go', 'glm-5.3', 'plan', 50000, 12000)]
        for gun, tutar in enumerate((180000, 420000, 90000, 610000, 300000, 740000), 1):
            satirlar.append(('openrouter', OR_DS_AD, 'g1', tutar, gun * 86400 + 3600))
        for i, (saglayici, model, gorev, tutar, once) in enumerate(satirlar):
            con.execute('insert into reservation values (?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, saglayici, time.strftime('%Y-%m-%d'), simdi - once,
                                                                            tutar, tutar, f'makbuz-{i}', json.dumps({'model': model, 'task_id': gorev})))
    with closing(sqlite3.connect(home / 'zincir' / 'aylik-butce.db')) as con, con:
        con.execute(sema)
        for i, (saglayici, tutar) in enumerate((('openrouter', 3_200_000), ('opencode', 220_000), ('opencode-go', 2_100_000))):
            con.execute('insert into reservation values (?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, saglayici, time.strftime('%Y-%m'), simdi - 3600 * (i + 1),
                                                                            tutar, tutar, f'aylik-{i}', '{}'))
    gecmis = [{'ts': simdi - (23 - i) * 3600, 'rolling': (17 * i) % 70, 'weekly': 70 + i * 13 // 23, 'monthly': 80 + i * 8 // 23} for i in range(24)]
    yaz(home / 'zincir' / 'go-pencere.json', {'ts': simdi, 'rolling': 57, 'weekly': 83, 'monthly': 88, 'gecmis': gecmis, 'resets': {
        'rolling': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(simdi + 2 * 3600)),
        'weekly': time.strftime('%Y-%m-%dT00:00:00.000Z', time.gmtime(simdi + 4 * 86400)),
        'monthly': time.strftime('%Y-%m-02T20:39:30.000Z', time.gmtime(simdi + (32 - time.gmtime(simdi).tm_mday) * 86400))}})
    yaz(home / 'zincir' / 'groq-gun-sayac.txt', f"{time.strftime('%Y-%m-%d')} 4\n")


def butce_sondasi(home: Path) -> tuple[dict, float]:
    return ({'ts': time.strftime('%Y-%m-%dT00:00:00'), 'bugun': time.strftime('%Y-%m-%d'),
             'go': {'kendi': {'rolling': 57, 'weekly': 83, 'monthly': 88}, 'kullanilabilir': 5, 'pencere_acik': True},
             'or': {'kalan': 61.72, 'kullanilabilir': 61.72}, 'zen': {'kalan': 4.47}, 'claude': {'used7': 71, 'used5': 4, 'tavan': 77.5}}, 40.0)


def zincir_sondasi(rol: str, b: dict | None = None, durum: dict | None = None) -> tuple[list, list]:
    import yonlendirici as Y
    adaylar = list(dict.fromkeys(m for aday in Y.ROLLER.get(rol, []) for m in aday.split('|')))
    nedenler = {'opencode-go': s("Go'nun cadre payı bu ay bitti, ayın 2'sinde açılır", "Go's cadre share is used up, it reopens on the 2nd"),
                'claude': s('Claude cadre içinde kapalı', 'Claude is off inside cadre')}
    zincir = [m for m in adaylar if m.split('/')[0] not in nedenler]
    return zincir, [f"{m}: {nedenler[m.split('/')[0]]}" for m in adaylar if m.split('/')[0] in nedenler]


def onay_sondasi(k: dict) -> bool:
    fr = k.get('final_result') or {}
    return str(fr.get('status')).upper() == 'PASS' and str((fr.get('review') or {}).get('status')).upper() == 'PASS'


def sondalari_kur() -> None:
    V.SONDA.update(butce=butce_sondasi, zincir=zincir_sondasi, onay=onay_sondasi, claude_kapali=lambda: True,
                   ariza=lambda: {OR_SOL: {'hata': 1, 'son': s('bugün 14:02 zaman aşımı, sonraki halkaya geçildi',
                                                              'today 14:02 timeout, moved to the next link')}})


def sondalari_kaldir() -> None:
    V.SONDA.clear()


def kur(home: Path | None = None) -> tuple[Path, dict]:
    home = Path(home or tempfile.mkdtemp(prefix='cadre-tasarim-'))
    simdi = time.time()
    repolar = {
        'feedloom': repo_kur(home, 'feedloom', {'README.md': '# feedloom\n', 'src/parser/tarih.py': 'def ayristir(metin):\n    return metin\n',
                                                  'src/kaynaklar/ortak.py': 'from src.parser.tarih import ayristir\n', 'BACKLOG.md': '# BACKLOG\n'}),
        'notlar-lab': repo_kur(home, 'notlar-lab', {'notlar.py': 'def notlar():\n    return []\n', 'README.md': '# notlar-lab\n',
                                                    'Makefile': 'check:\n\tpytest -q\n'}),
        'demo-lab': repo_kur(home, 'demo-lab', {'fiyat.py': 'def fiyat():\n    return 0\n', 'README.md': '# demo-lab\n'})}
    gecmis(home, repolar, simdi)
    sahne: dict[str, Any] = {'home': home}
    sahne.update(feedloom(home, repolar['feedloom'][0], simdi))
    sahne.update(notlar(home, repolar['notlar-lab'][0], simdi))
    defterler(home, simdi)
    return home, sahne


def temizle(home: Path) -> None:
    if home.name.startswith('cadre-tasarim-'):
        shutil.rmtree(home, ignore_errors=True)


KODCU_SATIRLARI = [
    ('arac', 'edit', {'filePath': 'src/parser/tarih.py'}),
    ('soz', 'Europe/Istanbul sabitlendi. Testi yeniden koşuyorum.', 'Pinned Europe/Istanbul. Running the test again.'),
    ('arac', 'bash', {'command': 'uv run pytest tests/test_tarih.py -q'}),
    ('soz', 'Dört test de yeşil. Kapsam dışı dosyaya dokunmadım.', 'All four tests are green. No out-of-scope file touched.'),
    ('arac', 'read', {'filePath': 'tests/test_tarih.py'}),
    ('soz', "Kenar durum: 'dün' gece yarısından hemen sonra. Ekliyorum.", "Edge case: 'yesterday' right after midnight. Adding it."),
]
HAKEM_SATIRLARI = [
    ('arac', 'bash', {'command': 'git diff --stat'}),
    ('soz', 'Değişiklik iki dosyada. Mevcut testler dokunulmamış.', 'Changes are in two files. Existing tests untouched.'),
    ('arac', 'read', {'filePath': 'notlar.py'}),
    ('arac', 'bash', {'command': 'make check'}),
    ('soz', 'Kabul maddeleri karşılanıyor. Karar: GEÇER.', 'Acceptance items are met. Verdict: PASS.'),
]
PLANLAYICI_SATIRLARI = [
    ('arac', 'read', {'filePath': 'src/rapor.py'}),
    ('soz', 'Özet için tarih alanı hazır. İki alt görev yeterli.', 'The date field is ready for the summary. Two subtasks are enough.'),
    ('arac', 'glob', {'pattern': 'tests/test_rapor*.py'}),
]


class Simulasyon:
    def __init__(self, sahne: dict) -> None:
        self.sahne = sahne
        self.tik = 0
        self.canli: list[tuple[Path, list]] = [(sahne['canli_g2'], KODCU_SATIRLARI)]
        self.siralar = {3: self.nl_test_gecti, 6: self.c3_g2_bitti, 9: self.nl_inceleme_bitti, 12: self.c3_g3_bitti, 15: self.c3_test_gecti}

    def adim(self) -> None:
        self.tik += 1
        adim = self.siralar.get(self.tik)
        if adim:
            adim()
        for deneme_dizini, satirlar in self.canli:
            tur, *icerik = satirlar[self.tik % len(satirlar)]
            olay_ = arac(time.time(), icerik[0], icerik[1]) if tur == 'arac' else soz(time.time(), s(icerik[0], icerik[1]))
            ekle(deneme_dizini / 'stdout.jsonl', json.dumps(olay_, ensure_ascii=False))
        for anahtar in ('c3', 'jr', 'nl'):
            kosu = self.sahne.get(anahtar)
            if kosu and json.loads((kosu / 'run.json').read_text()).get('state') not in V.TERMINAL:
                guncelle(kosu)

    def hakem_mi(self, deneme_dizini: Path) -> bool:
        return deneme_dizini.parent.parent.name == 'kalite'

    def nl_test_gecti(self) -> None:
        nl = self.sahne['nl']
        qa = qa_sonucu(nl, 'make check', 'PASS', ['ruff check . ok', 'mypy ok', '38 passed in 1.31s'], 7.4)
        gorev_guncelle(nl, 'qa', qa_result=qa)
        gorev_guncelle(nl, 'kalite', executors=[])
        hakem = deneme(nl, 'kalite', 'kalite', OR_SOL, [arac(time.time(), 'bash', {'command': 'git diff --stat'})])
        self.canli.append((hakem, HAKEM_SATIRLARI))
        olay(nl, time.time(), s('QA KAPISI: PASS (make check)', 'QA GATE: PASS (make check)'))
        olay(nl, time.time(), s('HAKEM basladi: openai/gpt-6.1-sol', 'REVIEWER started: openai/gpt-6.1-sol'))

    def nl_inceleme_bitti(self) -> None:
        nl = self.sahne['nl']
        kayit = json.loads((nl / 'run.json').read_text())
        repo = self.sahne['nl_repo']
        wt = calisma_agaci(self.sahne['home'], repo, nl.name, {
            'notlar.py': repo.joinpath('notlar.py').read_text() + '\n\ndef disa_aktar_csv(notlar, yol):\n    import csv\n'
                         "    with open(yol, 'w', newline='') as f:\n        csv.writer(f).writerows([[n] for n in notlar])\n",
            'tests/test_csv.py': 'def test_csv():\n    assert True\n'}, s('CSV dışa aktarma', 'CSV export'))
        yaz(nl / 'tasks' / 'kalite' / 'review-1.txt', s('Değişiklik iki dosyada.\n\nKarar: GEÇER\n', 'Changes are in two files.\n\nVerdict: PASS\n'))
        self.canli = [c for c in self.canli if not self.hakem_mi(c[0])]
        guncelle(nl, state='completed', worktree=str(wt), source_sha=self.sahne['nl_taban'], budget_spent_usd=0.38, final_result={
            'status': 'PASS', 'qa': kayit['tasks']['qa']['qa_result'], 'review': {
                'status': 'PASS', 'verdict': 'GECER', 'model': OR_SOL, 'independent': True, 'writers': ['deepseek-v4-pro'],
                'excluded_writers': [OR_DS], 'repairs': 0, 'repair_limit': 1, 'acceptance': [
                    {'id': 'original_request', 'status': 'PASS', 'evidence': s('disa_aktar_csv eklendi, 6 test yeşil.', 'disa_aktar_csv added, 6 tests green.')}]}})
        olay(nl, time.time(), s('KABUL SONUCU: PASS · BITTI', 'ACCEPTANCE RESULT: PASS · DONE'))

    def c3_g2_bitti(self) -> None:
        c3 = self.sahne['c3']
        gorev_guncelle(c3, 'g2', state='uretti')
        atama(c3, time.time(), c3.name, 'g2', OR_DS, 'uretti', 8)
        yeni = deneme(c3, 'g3', 'impl', OR_DS, [arac(time.time(), 'read', {'filePath': 'tests/test_tarih.py'})])
        gorev_guncelle(c3, 'g3', state='running', attempt_count=1, last_executor=OR_DS)
        self.canli = [(yeni, KODCU_SATIRLARI)] + [c for c in self.canli if self.hakem_mi(c[0])]
        olay(c3, time.time(), 'gorev g2 uretti')
        olay(c3, time.time(), s('gorev g3 basladi', 'task g3 started'))

    def c3_g3_bitti(self) -> None:
        c3 = self.sahne['c3']
        gorev_guncelle(c3, 'g3', state='uretti')
        atama(c3, time.time(), c3.name, 'g3', OR_DS, 'uretti', 6)
        gorev_guncelle(c3, 'qa', updated=time.time())
        self.canli = [c for c in self.canli if self.hakem_mi(c[0])]
        olay(c3, time.time(), 'gorev g3 uretti')
        olay(c3, time.time(), s('QA KAPISI basladi: uv run pytest tests/test_tarih.py -q', 'QA GATE started: uv run pytest tests/test_tarih.py -q'))

    def c3_test_gecti(self) -> None:
        c3, jr = self.sahne['c3'], self.sahne['jr']
        qa = qa_sonucu(c3, 'uv run pytest tests/test_tarih.py -q', 'PASS', ['4 passed in 0.38s'], 3.9)
        gorev_guncelle(c3, 'qa', qa_result=qa)
        guncelle(c3, state='implemented', final_result={'status': 'UYGULANDI', 'qa': qa, 'review': {'status': 'SKIPPED'},
                                                       'reason': 'proje-son-kabul-bekliyor'})
        proje = json.loads((jr / 'proje.json').read_text())
        proje['ozellikler'][2]['durum'] = 'gecti'
        proje['cocuklar'].append({'ozellik': 'f3', 'kosu': c3.name, 'durum': 'implemented', 'basari': True, 'neden': 'proje-son-kabul-bekliyor'})
        c4 = kosu_kur(self.sahne['home'], self.sahne['alt_proje'], time.time(), 'f4: ' + proje['ozellikler'][3]['aciklama'],
                      proje_ust=jr.name, proje_ozellik='f4', budget_cap_usd=2.0)
        planlayici = deneme(c4, 'plan', 'plan', OR_SOL, [arac(time.time(), 'read', {'filePath': 'BACKLOG.md'}),
                                                        soz(time.time(), s("f4 rapor özeti f3'ün tarih alanını kullanıyor. f3 bitti, başlıyorum.",
                                                                           'f4 uses the date field from f3. f3 is done, starting.'))])
        proje['ozellikler'][3].update(kosu=[c4.name], deneme=1)
        proje['son_cocuk'] = c4.name
        yaz(jr / 'proje.json', proje)
        guncelle(jr, project_active_feature='f4', budget_spent_usd=2.01)
        self.sahne['c3'] = c4
        self.canli = [(planlayici, PLANLAYICI_SATIRLARI)] + [c for c in self.canli if self.hakem_mi(c[0])]
        olay(c3, time.time(), s('QA KAPISI: PASS', 'QA GATE: PASS'))
        olay(jr, time.time(), s('f3 gecti · f4 basladi', 'f3 passed · f4 started'))
