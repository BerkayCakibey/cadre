#!/usr/bin/env python3
"""Textual full-screen operator panel (real data and a sample-data preview)."""
from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich import box  # noqa: E402
from rich.console import Group  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.progress_bar import ProgressBar as Cubuk  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import Input, Select, Static, TextArea  # noqa: E402

import arayuz_veri  # noqa: E402
import dil  # noqa: E402
import tasarim_ornek  # noqa: E402
from dil import t  # noqa: E402

PALETLER = {
    'Gruvbox': {'arka': '#32302f', 'yuzey': '#3c3836', 'cizgi': '#504945', 'yazi': '#ebdbb2', 'soluk': '#a89984', 'vurgu': '#d8a657',
                'iyi': '#a9b665', 'uyari': '#ef965b', 'kotu': '#ff8880', 'para': '#a9b665', 'model': '#df96af', 'zaman': '#89b482', 'kod': 'gruvbox-dark'},
    'Everforest': {'arka': '#2d353b', 'yuzey': '#343f44', 'cizgi': '#475258', 'yazi': '#d3c6aa', 'soluk': '#9da9a0', 'vurgu': '#dbbc7f',
                   'iyi': '#a7c080', 'uyari': '#e69875', 'kotu': '#f59395', 'para': '#a7c080', 'model': '#d699b6', 'zaman': '#83c092', 'kod': 'gruvbox-dark'},
    'Rosé Pine': {'arka': '#191724', 'yuzey': '#1f1d2e', 'cizgi': '#403d52', 'yazi': '#e0def4', 'soluk': '#908caa', 'vurgu': '#f6c177',
                  'iyi': '#9ccfd8', 'uyari': '#ebbcba', 'kotu': '#eb6f92', 'para': '#9ccfd8', 'model': '#c4a7e7', 'zaman': '#ebbcba', 'kod': 'dracula'},
    'Tokyo Night': {'arka': '#1a1b26', 'yuzey': '#24283b', 'cizgi': '#414868', 'yazi': '#c0caf5', 'soluk': '#9aa5ce', 'vurgu': '#7aa2f7',
                    'iyi': '#9ece6a', 'uyari': '#e0af68', 'kotu': '#f7768e', 'para': '#9ece6a', 'model': '#bb9af7', 'zaman': '#7dcfff', 'kod': 'monokai'},
    'Dracula': {'arka': '#282a36', 'yuzey': '#343746', 'cizgi': '#6272a4', 'yazi': '#f8f8f2', 'soluk': '#a6accd', 'vurgu': '#bd93f9',
                'iyi': '#50fa7b', 'uyari': '#f1fa8c', 'kotu': '#ff8585', 'para': '#50fa7b', 'model': '#ff79c6', 'zaman': '#8be9fd', 'kod': 'dracula'},
    'Catppuccin Mocha': {'arka': '#1e1e2e', 'yuzey': '#313244', 'cizgi': '#585b70', 'yazi': '#cdd6f4', 'soluk': '#a6adc8', 'vurgu': '#cba6f7',
                         'iyi': '#a6e3a1', 'uyari': '#f9e2af', 'kotu': '#f38ba8', 'para': '#a6e3a1', 'model': '#f5c2e7', 'zaman': '#89dceb', 'kod': 'dracula'},
}
R: dict[str, str] = {'ad': 'Gruvbox', **PALETLER['Gruvbox']}
ISARET = {'bitti': '●', 'simdi': '◉', 'bekliyor': '○', 'hata': '✗'}
BOLUMLER = (('panel', 'Panel'), ('gorevler', 'Görevler'), ('butce', 'Bütçe'), ('ajanlar', 'Ajanlar'), ('ayarlar', 'Ayarlar'))
MOTOR = Path(os.environ.get('CADRE_ENGINE_ROOT') or Path(__file__).resolve().parents[1])
SATIR_BASINA = 3
ANLAM = {
    'para': re.compile(r'\$\d+(?:[.,]\d+)?'),
    'model': re.compile(r'\b(?:[\w.-]+/)*(?:gpt|glm|deepseek|qwen|claude|kimi|gemini|ds|opus|sonnet)[\w./:-]*\b', re.I),
    'zaman': re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d+\s*(?:dk|min|sn|sa|h|s)\b'),
    'iyi': re.compile(r'^(?:bitti|tamam|geçti|geçer|done|passed|pass|completed)$', re.I),
    'kotu': re.compile(r'^(?:hata|düştü|başarısız|failed|fail|error)$', re.I),
    'uyari': re.compile(r'^(?:bekliyor|kuyrukta|engellendi|waiting|queued|blocked|cancelled|iptal edildi)$', re.I),
    'vurgu': re.compile(r'^(?:çalışıyor|sürüyor|running)$', re.I),
}


def palet_kur(ad: str | None = None) -> str:
    ad = ad if ad in PALETLER else dil.renk_oku()
    R.clear()
    R.update({'ad': ad, **PALETLER[ad]})
    return ad


def yazi(metin: str, stil: str = '') -> Text:
    sonuc = Text(metin, style=stil or R['yazi'])
    for tur, desen in ANLAM.items():
        for eslesme in desen.finditer(metin):
            sonuc.stylize(R[tur], eslesme.start(), eslesme.end())
    return sonuc


def renk(durum: str) -> str:
    return {'bitti': R['iyi'], 'simdi': R['vurgu'], 'hata': R['kotu']}.get(durum, R['soluk'])


def tek_satir(metin: Text) -> Text:
    metin.no_wrap, metin.overflow = True, 'ellipsis'
    return metin


def bar_metni(yuzde: float, genislik: int = 20) -> Text:
    dolu = round(genislik * max(0.0, min(float(yuzde), 100.0)) / 100)
    return Text.assemble(('━' * dolu, R['vurgu']), ('━' * (genislik - dolu), R['cizgi']))


def cubuk(ad: str, yuzde: float, genislik: int = 24) -> Table:
    satir = Table.grid(padding=(0, 2))
    satir.add_column(width=16, no_wrap=True)
    satir.add_column(width=genislik)
    satir.add_column()
    satir.add_row(yazi(ad, R['soluk']), Cubuk(total=100, completed=max(0.0, min(float(yuzde), 100.0)), width=genislik, style=R['cizgi'],
                                              complete_style=R['vurgu'], finished_style=R['vurgu']), yazi(arayuz_veri.oran(yuzde)))
    return satir


def bolum_cizgisi(baslik: str) -> Text:
    return Text.assemble(('\n── ', R['cizgi']), (baslik, f"bold {R['soluk']}"), (' ' + '─' * 60, R['cizgi']), no_wrap=True, overflow='crop')


def tahmini_satir(bloklar: list) -> tuple[int, int]:
    satir = alan = 0
    for tur, deger in bloklar:
        if tur == 'kayit':
            satir += max(1, len(deger or []))
        elif tur == 'bolum':
            satir += 2
        elif tur == 'satir':
            satir += 1 + len(str(deger)) // 70
        else:
            satir += 1
            alan += tur == 'alan'
    return satir, alan


def form(bloklar: list, ferah: bool = False) -> list:
    parcalar: list = []
    alanlar: list = []

    def alanlari_bosalt() -> None:
        if alanlar:
            tablo = Table.grid(padding=(0, 2))
            tablo.add_column(width=16, no_wrap=True)
            tablo.add_column(ratio=1)
            for sira, (etiket, deger) in enumerate(alanlar):
                if sira and ferah:
                    tablo.add_row('', '')
                tur = 'model' if 'model' in str(etiket).lower() else ''
                tablo.add_row(yazi(str(etiket), R['soluk']), yazi(str(deger), R[tur] if tur else ''))
            parcalar.append(tablo)
            alanlar.clear()
    for tur, deger in bloklar:
        if tur == 'alan':
            alanlar.append(deger)
            continue
        alanlari_bosalt()
        if tur == 'baslik':
            parcalar.append(yazi(deger, f"bold {R['vurgu']}"))
        elif tur == 'satir':
            parcalar.append(yazi(deger))
        elif tur == 'satir-kisa':
            parcalar.append(tek_satir(yazi(deger)))
        elif tur == 'soluk':
            parcalar.append(yazi(deger, R['soluk']))
        elif tur == 'kod':
            parcalar.append(yazi(str(deger), R.get('kod', '')))
        elif tur == 'cubuk':
            parcalar.append(cubuk(*deger))
        elif tur == 'bolum':
            parcalar.append(bolum_cizgisi(deger))
        elif tur == 'bolum-kisa':
            parcalar.append(tek_satir(bolum_cizgisi(deger)[1:]))
        elif tur == 'akis':
            satir = Text()
            for sira, (ad, durum) in enumerate(deger):
                if sira:
                    satir.append('  →  ', style=R['cizgi'])
                satir.append(f"{ISARET.get(durum, '·')} ", style=renk(durum))
                satir.append(ad, style=f"bold {R['yazi']}" if durum == 'simdi' else R['soluk'])
            parcalar += [satir, yazi('')]
        elif tur == 'kayit':
            tablo = Table.grid()
            tablo.add_column(width=2)
            tablo.add_column(ratio=1)
            for satir_ in deger or ['—']:
                tablo.add_row('', yazi(str(satir_), R['soluk']))
            parcalar.append(tablo)
    alanlari_bosalt()
    return parcalar


def arkada_baslat(komut: list[str], env: dict[str, str], kayit: Path) -> subprocess.Popen:
    kayit.parent.mkdir(parents=True, exist_ok=True)
    with open(kayit, 'ab') as cikti:
        return subprocess.Popen(komut, stdin=subprocess.DEVNULL, stdout=cikti, stderr=cikti, env=env, start_new_session=True)


class Sakin(VerticalScroll, can_focus=False):
    pass


def sakin_boya(w: VerticalScroll) -> None:
    w.styles.scrollbar_background = R['arka']
    w.styles.scrollbar_background_hover = R['arka']
    w.styles.scrollbar_background_active = R['arka']
    w.styles.scrollbar_color = R['cizgi']
    w.styles.scrollbar_color_hover = R['soluk']
    w.styles.scrollbar_color_active = R['vurgu']
    w.styles.scrollbar_size_vertical = 1


class Pencere(ModalScreen):
    def kutu_boya(self, kimlik: str) -> None:
        self.styles.background = R['arka']
        kutu = self.query_one(kimlik)
        kutu.styles.background = R['yuzey']
        kutu.styles.border = ('round', R['vurgu'])
        kutu.styles.border_title_color = R['vurgu']


class Onay(Pencere):
    """FIX-51: permission windows require a typed approval (`yazili=True`): Enter alone, a repeated held key or queued
    events cannot pass the step; `evet`/`yes` must be typed followed by Enter. The plain Approve (cancel/plan) carries a short dead time after opening."""
    BINDINGS = [Binding('enter', 'evet', show=False), Binding('q', 'app.pop_screen', show=False, priority=True),
                Binding('escape', 'app.pop_screen', show=False, priority=True), Binding('up', 'kaydir(-1)', show=False, priority=True),
                Binding('down', 'kaydir(1)', show=False, priority=True), Binding('pageup', 'kaydir(-10)', show=False, priority=True),
                Binding('pagedown', 'kaydir(10)', show=False, priority=True), Binding('end', 'kaydir(100000)', show=False, priority=True)]
    DEFAULT_CSS = '''
    Onay { align: center middle; }
    #onay { width: 72; max-width: 94%; height: auto; max-height: 94%; padding: 1 3; }
    #onay.uzun { height: 90%; }
    #onay-icerik { height: auto; }
    #onay.uzun #onay-icerik { height: 1fr; }
    #onay-yazi { margin-top: 1; }
    #onay-ipucu { height: auto; padding-top: 1; }
    '''

    OLU_SURE_SN = float(os.environ.get('CADRE_ONAY_OLU_SURE_SN', '0.3'))
    ONAY_SOZLERI = ('evet', 'e', 'yes', 'y')

    def __init__(self, soru: str, ayrinti: str, eylem, yazili: bool = False) -> None:
        super().__init__()
        self.soru, self.ayrinti, self.eylem, self.yazili = soru, ayrinti, eylem, yazili
        self.acilis = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(id='onay', classes='uzun' if len(self.ayrinti) > 700 else ''):
            yield Static(yazi(self.soru, f"bold {R['yazi']}"))
            with VerticalScroll(id='onay-icerik'):
                yield Static(yazi(self.ayrinti, R['soluk']))
            if self.yazili:
                yield Input(id='onay-yazi', placeholder=t('onaylamak için evet yaz'))
            yield Static(yazi(t('evet yazıp Enter      q  hayır') if self.yazili else t('Enter  evet      q  hayır'), R['soluk']), id='onay-ipucu')

    def on_mount(self) -> None:
        self.kutu_boya('#onay')
        self.acilis = time.monotonic()
        if self.yazili:
            self.query_one('#onay-yazi', Input).focus()

    def yazili_onay(self) -> bool:
        return not self.yazili or self.query_one('#onay-yazi', Input).value.strip().lower() in self.ONAY_SOZLERI

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_evet()

    def action_evet(self) -> None:
        if time.monotonic() - self.acilis < self.OLU_SURE_SN or not self.yazili_onay():
            return
        self.app.pop_screen()
        self.eylem()

    def action_kaydir(self, amount: int) -> None:
        self.query_one('#onay-icerik').scroll_relative(y=amount, animate=False)


class ButceAyari(Pencere):
    BINDINGS = [Binding('enter', 'ileri', priority=True, show=False), Binding('escape', 'app.pop_screen', priority=True, show=False),
                Binding('ctrl+e', 'duzenle', priority=True, show=False)]
    DEFAULT_CSS = '''
    ButceAyari { align: center middle; }
    #ba-kutu { width: 76; max-width: 94%; height: auto; max-height: 100%; padding: 1 2; }
    #ba-deger { margin: 1 0; }
    '''

    def __init__(self, home: Path, state: dict) -> None:
        super().__init__()
        self.home, self.state, self.onizleme = home, state, False
        self.micro = 0

    def compose(self) -> ComposeResult:
        with Vertical(id='ba-kutu'):
            yield Static(id='ba-bilgi')
            yield Input(value=format(Decimal(self.state['mikro']) / 1_000_000, 'f'), id='ba-deger')
            yield Static(id='ba-hata')
            yield Static(id='ba-ipucu')

    def on_mount(self) -> None:
        self.kutu_boya('#ba-kutu')
        self.ciz()
        self.query_one(Input).focus()

    def ciz(self) -> None:
        scope = self.state['proje'] if self.state['tur'] == 'kosu' else t('Ortak aylık nakit tavanı')
        lines = [yazi(scope, f"bold {R['vurgu']}"), yazi(t('Mevcut: {v} USD', v=self.state['deger']))]
        if self.onizleme:
            lines.append(yazi(t('Yeni: {v} USD', v=format(Decimal(self.micro) / 1_000_000, 'f')), R['para']))
            if self.micro > self.state['mikro']:
                lines.append(yazi(t('Tavan artışı: Enter bu artışı açıkça onaylar.'), R['uyari']))
        lines.append(yazi(t('Mevcut koşu tavanı değişmez. Yeni görev için kesin izin ayrıca gerekir.') if self.state['tur'] == 'kosu'
                          else t('Sonraki nakit çağrılarında uygulanır. Harcama ve rezervler korunur.'), R['soluk']))
        self.query_one('#ba-bilgi', Static).update(Group(*lines))
        self.query_one(Input).display = not self.onizleme
        self.query_one('#ba-ipucu', Static).update(yazi(t('Enter  kaydet      Ctrl+E  düzenle      Esc  vazgeç') if self.onizleme
                                                        else t('USD · Enter  özeti gör      Esc  vazgeç'), R['soluk']))

    def action_duzenle(self) -> None:
        self.onizleme = False
        self.ciz()
        self.query_one(Input).focus()

    def action_ileri(self) -> None:
        from aylik_butce import MonthlyBudgetDenied, usd_micro
        from kosu import butce_ayari_yaz
        try:
            if not self.onizleme:
                self.micro = usd_micro(self.query_one(Input).value, zero=self.state['tur'] == 'kosu')
                self.onizleme = True
                self.query_one('#ba-hata', Static).update('')
                self.ciz()
                return
            butce_ayari_yaz(self.home, self.state['tur'], self.state['proje'], format(Decimal(self.micro) / 1_000_000, 'f'), self.state)
        except (MonthlyBudgetDenied, ValueError, OSError):
            self.query_one('#ba-hata', Static).update(yazi(t('Kaydedilmedi: tutarı, ortam önceliğini ve değişen kaynağı kontrol et.'), R['kotu']))
            return
        self.app.pop_screen()
        arayuz_veri.temizle()
        self.app.yenile(True)


class GorevVarsayilanlari(Pencere):
    BINDINGS = [Binding('ctrl+s', 'kaydet', priority=True, show=False), Binding('escape', 'app.pop_screen', priority=True, show=False)]
    DEFAULT_CSS = '''
    GorevVarsayilanlari { align: center middle; }
    #gv-kutu { width: 76; max-width: 94%; height: auto; max-height: 100%; padding: 1 2; }
    #gv-kutu Static { height: auto; }
    '''

    def __init__(self, home: Path, state: dict) -> None:
        super().__init__()
        self.home, self.state = home, state

    def compose(self) -> ComposeResult:
        with Vertical(id='gv-kutu'):
            yield Static(t('Yeni görev varsayılanları'))
            yield Static(t('Maliyet tercihi'))
            yield Select([(t('Ekonomik'), 'economic'), (t('Dengeli'), 'balanced'), (t('Güçlü'), 'strong')],
                         value=self.state['preference'], allow_blank=False, id='gv-preference')
            yield Static(t('Zorluk düzeltmesi'))
            yield Select([(t('Otomatik tahmin bekleniyor'), 'auto'), (t('Kolay'), 'easy'), (t('Orta'), 'medium'),
                          (t('Zor'), 'hard'), (t('Çok zor'), 'very_hard')], value=self.state['difficulty_override'] or 'auto',
                         allow_blank=False, id='gv-difficulty')
            yield Static(t('Yeni görev formunu doldurur. Mevcut görev, izin ve bütçe değişmez.'))
            yield Static(id='gv-hata')
            yield Static(t('Ctrl+S  kaydet      Esc  vazgeç'))

    def on_mount(self) -> None:
        self.kutu_boya('#gv-kutu')

    def action_kaydet(self) -> None:
        from kosu import gorev_varsayilanlari_yaz
        difficulty = str(self.query_one('#gv-difficulty', Select).value)
        try:
            gorev_varsayilanlari_yaz(self.home, str(self.query_one('#gv-preference', Select).value),
                                    None if difficulty == 'auto' else difficulty, self.state)
        except (ValueError, OSError):
            self.query_one('#gv-hata', Static).update(yazi(t('Kaydedilmedi. Ayarlar değişmiş veya okunamıyor. Düzenleyiciyi yeniden aç.'), R['kotu']))
            return
        self.app.pop_screen()
        arayuz_veri.temizle()
        self.app.yenile(True)


class Okuyucu(Pencere):
    BINDINGS = [Binding('q', 'app.pop_screen', show=False), Binding('escape', 'app.pop_screen', show=False),
                Binding('left', 'app.pop_screen', show=False), Binding('o', 'app.pop_screen', show=False),
                Binding('up', 'kaydir(-1)', show=False), Binding('down', 'kaydir(1)', show=False),
                Binding('shift+up', 'sayfa(-1)', show=False), Binding('shift+down', 'sayfa(1)', show=False),
                Binding('pageup', 'sayfa(-1)', show=False), Binding('pagedown', 'sayfa(1)', show=False), Binding('space', 'sayfa(1)', show=False),
                Binding('r', 'ham', show=False)]
    DEFAULT_CSS = """
    Okuyucu { align: center middle; }
    #ok-kutu { width: 100%; height: 100%; padding: 0 1; }
    #ok-govde { height: 1fr; }
    #ok-ipucu { height: 1; }
    """

    def __init__(self, baslik: str, tur: str, metin: str, kaynak: dict | None = None) -> None:
        super().__init__()
        self.baslik, self.tur, self.metin = baslik, tur, metin
        self.kaynak, self.ham = kaynak, False

    def compose(self) -> ComposeResult:
        with Vertical(id='ok-kutu'):
            yield Sakin(Static(id='ok-metin'), id='ok-govde')
            yield Static(id='ok-ipucu')

    def on_mount(self) -> None:
        self.kutu_boya('#ok-kutu')
        sakin_boya(self.query_one('#ok-govde', Sakin))
        self.boya()

    def boya(self) -> None:
        self.query_one('#ok-kutu').border_title = self.baslik
        if self.tur == 'md':
            icerik = Markdown(self.metin)
        elif self.tur in ('diff', 'json'):
            icerik = Syntax(self.metin, self.tur, theme=R['kod'], word_wrap=True, background_color=R['yuzey'])
        else:
            icerik = Text(self.metin, style=R['yazi'])
        self.query_one('#ok-metin', Static).update(icerik)
        ipucu = t('↑ ↓  kaydır      Shift+↑ ↓  sayfa      q  kapat')
        if self.kaynak and self.kaynak.get('tur') in ('beyin', 'jsonl', 'log', 'iz'):
            ipucu = t('Maskeli ham kayıt' if self.ham else 'Okunur kayıt') + ' · ' + t('r  görünüm') + ' · ' + ipucu
        self.query_one('#ok-ipucu', Static).update(yazi(ipucu, R['soluk']))

    def action_ham(self) -> None:
        if self.kaynak and self.kaynak.get('tur') in ('beyin', 'jsonl', 'log', 'iz'):
            self.ham = not self.ham
            self.baslik, self.tur, self.metin = arayuz_veri.dosya_oku(self.kaynak, ham=self.ham)
            self.boya()
            self.query_one('#ok-govde', Sakin).scroll_home(animate=False)

    def action_kaydir(self, d: int) -> None:
        self.query_one('#ok-govde', Sakin).scroll_relative(y=d, animate=False)

    def action_sayfa(self, d: int) -> None:
        kutu = self.query_one('#ok-govde', Sakin)
        (kutu.scroll_page_down if d > 0 else kutu.scroll_page_up)(animate=False)


class CanliIzleme(Pencere):
    BINDINGS = [Binding('q', 'app.pop_screen', show=False, priority=True),
                Binding('escape', 'app.pop_screen', show=False, priority=True),
                Binding('up', 'sec(-1)', show=False), Binding('down', 'sec(1)', show=False),
                Binding('o', 'oku', show=False), Binding('pageup', 'sayfa(-1)', show=False), Binding('pagedown', 'sayfa(1)', show=False)]
    DEFAULT_CSS = '''
    CanliIzleme { align: center middle; }
    #iz-kutu { width: 100%; height: 100%; padding: 0 1; }
    #iz-durum { height: auto; max-height: 6; }
    #iz-icerik { height: 1fr; }
    #iz-liste { width: 35%; height: 1fr; margin-right: 1; }
    #iz-ayrinti { width: 1fr; height: 1fr; }
    #iz-ipucu { height: 1; }
    '''

    def __init__(self, home: Path, run_id: str) -> None:
        super().__init__()
        self.home, self.run_id = home, run_id
        self.veri: dict = {}
        self.ogeler: list[tuple[tuple[str, ...], dict]] = []
        self.secim, self.okundu, self.hata, self.yukleniyor = 0, 0.0, '', False

    def compose(self) -> ComposeResult:
        with Vertical(id='iz-kutu'):
            yield Static(id='iz-durum')
            with Horizontal(id='iz-icerik'):
                yield Sakin(Static(id='iz-menu'), id='iz-liste')
                yield Sakin(Static(id='iz-metin'), id='iz-ayrinti')
            yield Static(id='iz-ipucu')

    def on_mount(self) -> None:
        self.kutu_boya('#iz-kutu')
        self.query_one('#iz-kutu').border_title = f"{t('Canlı izleme')} · {self.run_id}"
        for name in ('#iz-liste', '#iz-ayrinti'):
            sakin_boya(self.query_one(name, Sakin))
        self.query_one('#iz-liste').styles.border_right = ('solid', R['cizgi'])
        self.set_interval(2, self.yenile)
        self.set_interval(1, self.boya)
        self.yenile()

    def yenile(self) -> None:
        if self.yukleniyor:
            return
        self.yukleniyor = True
        self.run_worker(self.yukle, thread=True, exit_on_error=False)

    def yukle(self) -> None:
        try:
            veri, hata = arayuz_veri.izleme_verisi(self.home, self.run_id), ''
        except Exception as exc:
            veri, hata = {}, arayuz_veri.hata_yazi(exc)
        if self.is_mounted:
            self.app.call_from_thread(self.veri_kur, veri, hata)

    def veri_kur(self, veri: dict, hata: str) -> None:
        if not self.is_mounted:
            return
        self.yukleniyor, self.hata = False, hata
        if not hata:
            selected = self.ogeler[self.secim][0] if self.ogeler else None
            self.veri, self.okundu, self.ogeler = veri, time.time(), []

            def ekle(node: dict, path: tuple[str, ...]) -> None:
                self.ogeler.append((path, node))
                for child in node['cocuk']:
                    ekle(child, (*path, str(child.get('id') or child['ad'])))

            ekle(veri, (self.run_id,))
            self.secim = next((i for i, (path, _) in enumerate(self.ogeler) if path == selected), min(self.secim, len(self.ogeler) - 1))
        self.boya()

    def boya(self) -> None:
        if not self.is_mounted:
            return
        durum = []
        if self.hata:
            durum.append(yazi(t('Eski veri · okuma hatası: {e}', e=self.hata), R['kotu']))
        if self.veri:
            durum.append(tek_satir(yazi(self.veri['ad'] + ' · ' + self.veri['ozet'])))
            def age(ts: float | None) -> str:
                if not ts:
                    return t('Zaman bilinmiyor')
                seconds = max(0, time.time() - ts)
                return t('{s} sn', s=int(seconds)) if seconds < 60 else arayuz_veri.sure_metni(seconds)
            durum.append(tek_satir(yazi(t('Son okuma: {o} · Koşu kaydının yaşı: {k} · Son eylem: {s}',
                                                       o=age(self.okundu), k=age(self.veri.get('kaynak_zamani')), s=age(self.veri.get('son_eylem_zamani'))), R['soluk'])))
            durum += form(self.veri.get('baglam', [])[-3:-1])
        else:
            durum.append(yazi(t('Kayıt bekleniyor'), R['soluk']))
        self.query_one('#iz-durum', Static).update(Group(*durum))
        menu = []
        width = max(1, self.query_one('#iz-liste').size.width - 2)
        for i, (path, node) in enumerate(self.ogeler):
            style = f"bold {R['arka']} on {R['vurgu']}" if i == self.secim else R['yazi']
            line = Text('  ' * (len(path) - 1) + f"{ISARET.get(node['durum'], '·')} {node['ad']}", style=style)
            line.truncate(width, overflow='ellipsis', pad=True)
            menu.append(tek_satir(line))
        self.query_one('#iz-menu', Static).update(Group(*menu))
        if self.ogeler:
            node = self.ogeler[self.secim][1]
            self.query_one('#iz-metin', Static).update(Group(*form(node['ayrinti']),
                *form([('bolum', t('Son eylemler')), ('alan', (t('Kaynak'), self.veri['son_eylem_kaynagi'])), ('kayit', self.veri['son_eylemler'])])))
        self.query_one('#iz-ipucu', Static).update(yazi(t('↑ ↓  seç      o  kayıt      PgUp/PgDn  ayrıntı      q/Esc  kapat · iş sürer'), R['soluk']))

    def action_sec(self, delta: int) -> None:
        if self.ogeler:
            self.secim = (self.secim + delta) % len(self.ogeler)
            self.boya()
            self.query_one('#iz-ayrinti', Sakin).scroll_home(animate=False)
            self.query_one('#iz-liste', Sakin).scroll_to(y=max(0, self.secim - 2), animate=False)

    def action_oku(self) -> None:
        spec = self.ogeler[self.secim][1].get('dosya') if self.ogeler else None
        if spec:
            self.app.push_screen(Okuyucu(*arayuz_veri.dosya_oku(spec), kaynak=spec))

    def action_sayfa(self, delta: int) -> None:
        view = self.query_one('#iz-ayrinti', Sakin)
        (view.scroll_page_down if delta > 0 else view.scroll_page_up)(animate=False)


class PlanDegisikligi(Pencere):
    BINDINGS = [Binding('ctrl+s', 'gonder', show=False), Binding('escape', 'vazgec', show=False)]
    DEFAULT_CSS = 'PlanDegisikligi { align: center middle; } #plan-change-form { width: 78; max-width: 94%; height: auto; max-height: 90%; padding: 1 2; } #plan-change-context { height: auto; max-height: 8; } #plan-change-text { height: 7; }'

    def __init__(self, home: Path, run_id: str, revision: int, decision: dict[str, str] | None = None) -> None:
        super().__init__()
        self.home, self.run_id, self.revision = home, run_id, revision
        self.change_id = uuid.uuid4().hex
        self.decision = decision

    def compose(self) -> ComposeResult:
        with Vertical(id='plan-change-form'):
            yield Static(t('Kritik kararı yanıtla') if self.decision else t('Plan değişikliği iste'))
            with Sakin(id='plan-change-context'):
                yield Static(arayuz_veri.metin_redakte(self.decision['question']) if self.decision else
                             t('Önce yeni adımlar durdurulur. Gerçek duruş ayrıca doğrulanır.'), markup=False, id='plan-change-question')
            if self.decision:
                yield Static(t('Yanıt yeni izin veya bütçe artışı sayılmaz. Yeni plan ayrıca incelenir.'))
            yield TextArea(id='plan-change-text')
            yield Static('', id='plan-change-error')
            yield Static(t('Ctrl+S gönder · Esc vazgeç'))

    def on_mount(self) -> None:
        self.kutu_boya('#plan-change-form')
        self.query_one('#plan-change-text', TextArea).focus()

    def action_gonder(self) -> None:
        text = self.query_one('#plan-change-text', TextArea).text
        base = ['python3', str(MOTOR / 'lib/kosu.py')]
        command = ([*base, 'plan-answer', self.run_id, str(self.revision), self.decision['id']] if self.decision
                   else [*base, 'plan-change', self.run_id, self.change_id, str(self.revision)])
        try:
            result = subprocess.run(command, input=text, capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'CADRE_ENGINE_ROOT': str(MOTOR), 'CADRE_HOME': str(self.home)})
        except (OSError, subprocess.SubprocessError) as exc:
            self.query_one('#plan-change-error', Static).update(t('Komut yanıt vermedi: {e}', e=arayuz_veri.hata_yazi(exc)))
            return
        if result.returncode != 0:
            self.query_one('#plan-change-error', Static).update(
                t('Talep kaydedilmedi. Metni, plan sürümünü ve mevcut talebi kontrol et.') + '\n' + (result.stderr or result.stdout).strip()[-200:])
            return
        self.dismiss()
        self.app.yenile(True)

    def action_vazgec(self) -> None:
        self.dismiss()


class YeniGorev(Pencere):
    BINDINGS = [Binding('escape', 'vazgec', priority=True), Binding('ctrl+s', 'bitir', priority=True),
                Binding('ctrl+b', 'geri', priority=True),
                Binding('up', 'kaydir(-1)', show=False), Binding('down', 'kaydir(1)', show=False),
                Binding('enter', 'ileri', show=False), Binding('q', 'duzenle', show=False)]
    DEFAULT_CSS = """
    YeniGorev { align: center middle; }
    #yg-kutu { width: 90; max-width: 96%; height: auto; max-height: 100%; padding: 1 2; }
    #yg-metin { height: 8; margin: 1 0; }
    #yg-klasor { margin: 1 0; }
    #yg-ipucu { margin-top: 1; }
    #yg-scroll { height: 1fr; }
    #yg-fields, #yg-source { height: auto; }
    #yg-fields Static, #yg-source Static { height: auto; }
    #yg-kutu { height: 90%; }
    """

    def __init__(self, secenekler: list[dict], secili: int = 0) -> None:
        super().__init__()
        self.secenekler = secenekler
        self.adim: int | str = 1
        self.secim = secili
        self.klasor = ''
        self.submission_id = uuid.uuid4().hex
        self.request: dict = {}
        self.gonderiliyor = False
        self.butce_yuklendi = False
        self.varsayilan_state: dict | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id='yg-kutu'):
            yield Static(id='yg-baslik')
            with VerticalScroll(id='yg-scroll'):
                yield Static(id='yg-govde')
                yield Input(id='yg-klasor', placeholder=t('klasör adı ya da tam yol'))
                with Vertical(id='yg-source'):
                    yield Static(t('Girdi kipi'))
                    yield Select([(t('Hedefim var, planla'), 'goal'), (t('Hazır backlog'), 'backlog'),
                                  (t('Yalnız plan oluştur'), 'plan_only')], value='goal', allow_blank=False, id='yg-mode')
                    yield Input(id='yg-backlog', placeholder=t('Kaynak bölümü (isteğe bağlı): dosya.md#KART-ID'))
                    yield Input(id='yg-sources', placeholder=t('Okunacak kaynaklar (isteğe bağlı, inceleme): yol, yol'))
                yield TextArea(id='yg-metin')
                with Vertical(id='yg-fields'):
                    yield Static(id='yg-defaults')
                    yield Static(t('İstenen çıktı'))
                    yield Input(id='yg-output', value=t('İstekte tanımlanan çıktı'))
                    yield Static(t('Kabul kriterleri (isteğe bağlı)'))
                    yield Input(id='yg-acceptance')
                    yield Static(t('İzinli dosyalar (isteğe bağlı, dar kabul)'))
                    yield Input(id='yg-scope-files', placeholder=t('yol, yol — boşsa bütün paket kabulü'))
                    yield Static(t('Gerekli testler (isteğe bağlı, dar kabul)'))
                    yield Input(id='yg-scope-tests', placeholder=t('test dosyası, test dosyası'))
                    yield Static(t('Maliyet tercihi'))
                    yield Select([(t('Ekonomik'), 'economic'), (t('Dengeli'), 'balanced'), (t('Güçlü'), 'strong')], value='balanced', allow_blank=False, id='yg-preference')
                    yield Static(t('Zorluk düzeltmesi'))
                    yield Select([(t('Otomatik tahmin bekleniyor'), 'auto'), (t('Kolay'), 'easy'), (t('Orta'), 'medium'),
                                  (t('Zor'), 'hard'), (t('Çok zor'), 'very_hard')], value='auto', allow_blank=False, id='yg-difficulty')
                    yield Static(t('Toplam iş bütçesi (USD)'))
                    yield Input(id='yg-budget', value='2')
                    yield Static(t('Süre sınırı (dakika)'))
                    yield Input(id='yg-duration', value='60')
            yield Static(id='yg-ipucu')

    def on_mount(self) -> None:
        self.kutu_boya('#yg-kutu')
        from kosu import gorev_varsayilanlari
        try:
            self.varsayilan_state = gorev_varsayilanlari(self.app.home or arayuz_veri.ev())
            self.query_one('#yg-preference', Select).value = self.varsayilan_state['preference']
            self.query_one('#yg-difficulty', Select).value = self.varsayilan_state['difficulty_override'] or 'auto'
            preference = t({'economic': 'Ekonomik', 'balanced': 'Dengeli', 'strong': 'Güçlü'}[self.varsayilan_state['preference']])
            difficulty = t({None: 'Otomatik tahmin bekleniyor', 'easy': 'Kolay', 'medium': 'Orta', 'hard': 'Zor', 'very_hard': 'Çok zor'}[self.varsayilan_state['difficulty_override']])
            self.query_one('#yg-defaults', Static).update(yazi(t('Kayıtlı varsayılan: {p} · {d}', p=preference, d=difficulty) + '\n' +
                                                             t('Görünür varsayılanlar aşağıdadır. Düzenleyebilirsin, kesin izin sayılmaz.'), R['soluk']))
        except (ValueError, OSError):
            self.query_one('#yg-defaults', Static).update(yazi(t('Görev varsayılanları okunamadı. Ayarları kontrol et.'), R['kotu']))
        self.ciz()

    @property
    def hedef(self) -> dict:
        return self.secenekler[self.secim]

    def klasor_yolu(self) -> str:
        yol = Path(self.klasor).expanduser()
        return str(yol if yol.is_absolute() else Path(os.getcwd()) / yol)

    def hedef_yazisi(self) -> str:
        if self.hedef['tur'] == 'yeni':
            return t('yeni klasör · {y}', y=self.klasor_yolu())
        if self.hedef['tur'] == 'bu':
            return t('bu klasör · {y}', y=self.hedef['yol'])
        return self.hedef['ad']

    def ciz(self) -> None:
        metin = self.query_one('#yg-metin', TextArea)
        klasor = self.query_one('#yg-klasor', Input)
        baslik = {1: t('1 / 3   Hedef ve kaynak'), 'ad': t('1 / 3   Yeni klasörün adı'), 2: t('2 / 3   Beklenti ve sınırlar'), 3: t('3 / 3   Özet ve gönder')}[self.adim]
        self.query_one('#yg-baslik', Static).update(yazi(t('Yeni görev') + '   ·   ' + baslik, f"bold {R['yazi']}"))
        if self.adim == 1:
            govde = Group(*self.kesit([tek_satir(Text.assemble(('› ' if i == self.secim else '  ', R['vurgu']),
                                                               (s['etiket'], f"bold {R['vurgu']}" if i == self.secim else R['yazi']),
                                                               (f"   {s.get('not', '')}", R['soluk']))) for i, s in enumerate(self.secenekler)],
                                      self.alan(15)))
            ipucu = t('↑ ↓  proje · Tab  alan · Ctrl+S  devam · Esc  vazgeç')
        elif self.adim == 'ad':
            govde = yazi(t('Kısa bir ad yazarsan şu an bulunduğun klasörün içinde açılır: {y}', y=os.getcwd()), R['soluk'])
            ipucu = t('yaz      Enter  devam      Esc  vazgeç')
        elif self.adim == 2:
            govde = yazi(t('nerede: {h}   ·   istediğin kadar satır yazabilirsin', h=self.hedef_yazisi()), R['soluk'])
            ipucu = t('Tab  alan · Ctrl+S  özet · Ctrl+B  geri · Esc  vazgeç')
        else:
            raw = self.request or self.girdi()
            govde = Group(yazi(t('nerede: {h}', h=self.hedef_yazisi())),
                          yazi(self.kisalt(metin.text.strip() or t('(boş)'), max(3, self.alan(17))), f"bold {R['yazi']}"),
                          *form(self.ozet_alanlari(raw)),
                          yazi(t('Bu özet kullanıcı girdisidir. Model planı henüz yok.'), R['soluk']),
                          yazi(t('Kayıt plan ve izin kapılarını bekler. Yürütücü başlamaz.'), R['uyari']))
            ipucu = t('Enter  gönder      q  yazmaya dön      Esc  vazgeç')
        self.query_one('#yg-govde', Static).update(govde)
        klasor.display = self.adim == 'ad'
        self.query_one('#yg-source').display = self.adim == 1
        self.query_one('#yg-fields').display = self.adim == 2
        self.query_one('#yg-backlog').display = self.query_one('#yg-mode', Select).value in ('goal', 'backlog')
        self.query_one('#yg-sources').display = self.query_one('#yg-mode', Select).value == 'goal'
        metin.display = self.adim == 2
        metin.styles.height = max(3, min(8, self.alan(11)))
        self.query_one('#yg-ipucu', Static).update(yazi(ipucu, R['soluk']))
        if self.adim == 'ad':
            klasor.focus()
        elif self.adim == 2:
            metin.focus()
        else:
            self.set_focus(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == 'yg-mode' and self.is_mounted:
            alan = self.query_one('#yg-backlog', Input)
            alan.display = event.value in ('goal', 'backlog')
            alan.placeholder = t('Backlog yolu (repo içinde)' if event.value == 'backlog'
                                 else 'Kaynak bölümü (isteğe bağlı): dosya.md#KART-ID')
            alan.value = ''
            kaynaklar = self.query_one('#yg-sources', Input)
            kaynaklar.display = event.value == 'goal'
            kaynaklar.value = ''

    def girdi(self) -> dict:
        home = self.app.home or arayuz_veri.ev()
        target = self.klasor_yolu() if self.hedef['tur'] == 'yeni' else self.hedef.get('yol', '')
        project = self.hedef.get('ad', '')
        if self.hedef['tur'] == 'proje':
            try:
                target = (home / 'projects' / project / 'repo-path').read_text().strip()
            except OSError:
                target = ''
        elif self.hedef['tur'] == 'bu':
            import proje_kayit
            project = proje_kayit.kayitli(home, Path(target).resolve()) or ''
        else:
            project = re.sub(r'[^a-z0-9]+', '-', Path(target).name.lower()).strip('-') or 'proje'
        difficulty = str(self.query_one('#yg-difficulty', Select).value)
        return {'submission_id': self.submission_id, 'project': project, 'target_path': target,
                'target_kind': 'new' if self.hedef['tur'] == 'yeni' else 'existing',
                'input_mode': str(self.query_one('#yg-mode', Select).value), 'backlog_path': self.query_one('#yg-backlog', Input).value,
                'source_paths': self.query_one('#yg-sources', Input).value,
                'prompt': self.query_one('#yg-metin', TextArea).text, 'output': self.query_one('#yg-output', Input).value,
                'acceptance': self.query_one('#yg-acceptance', Input).value, 'preference': str(self.query_one('#yg-preference', Select).value),
                'scope_files': self.query_one('#yg-scope-files', Input).value, 'scope_tests': self.query_one('#yg-scope-tests', Input).value,
                'difficulty_override': None if difficulty == 'auto' else difficulty, 'budget_usd': self.query_one('#yg-budget', Input).value,
                'duration_minutes': self.query_one('#yg-duration', Input).value, 'source_token': '',
                'defaults_token': self.varsayilan_state['token'] if self.varsayilan_state else ''}

    def ozet_alanlari(self, raw: dict) -> list:
        from arayuz_veri import kapsam_metni
        return [('alan', (t('Girdi kipi'), t({'goal': 'Hedefim var, planla', 'backlog': 'Hazır backlog', 'plan_only': 'Yalnız plan oluştur'}[raw['input_mode']]))),
                ('alan', (t('Kaynak bölümü' if raw['input_mode'] == 'goal' else 'Backlog'), raw['backlog_path'] or '—')),
                ('alan', (t('Okunacak kaynaklar'), raw.get('source_paths') or t('Sınırsız (proje kaynağı)'))),
                ('alan', (t('İstenen çıktı'), raw['output'])),
                ('alan', (t('Kabul kriterleri'), raw['acceptance'] or t('Planda ilk istekten türetilecek'))),
                ('alan', (t('Dar kabul kapsamı'), kapsam_metni(raw.get('scope_files'), raw.get('scope_tests')))),
                ('alan', (t('Maliyet tercihi'), t({'economic': 'Ekonomik', 'balanced': 'Dengeli', 'strong': 'Güçlü'}[raw['preference']]))),
                ('alan', (t('Zorluk düzeltmesi'), t({None: 'Otomatik tahmin bekleniyor', 'easy': 'Kolay', 'medium': 'Orta', 'hard': 'Zor', 'very_hard': 'Çok zor'}[raw['difficulty_override']]))),
                ('alan', (t('İş bütçesi'), raw['budget_usd'] + ' USD · ' + t('Toplam'))),
                ('soluk', t('Sağlayıcı kapsamı ve kesin izinler plan kapısında doğrulanacak.')),
                ('alan', (t('Süre'), t('{d} dk', d=raw['duration_minutes'])))]

    def alan(self, sabit: int) -> int:
        return max(3, int(self.app.size.height * 0.96) - sabit)

    def genislik(self) -> int:
        return max(20, min(90, int(self.app.size.width * 0.96)) - 8)

    def kesit(self, satirlar: list, sigan: int) -> list:
        if len(satirlar) <= sigan:
            return satirlar
        sigan = max(sigan - 2, 1)
        bas = min(max(0, self.secim - sigan // 2), len(satirlar) - sigan)
        kalan = len(satirlar) - bas - sigan
        return ([yazi(t('  ▲ {n} tane yukarıda', n=bas), R['soluk'])] if bas else [yazi('')]) + satirlar[bas:bas + sigan] + (
            [yazi(t('  ▼ {n} tane aşağıda', n=kalan), R['soluk'])] if kalan else [yazi('')])

    def kisalt(self, metin: str, sigan: int) -> str:
        genislik, sigan = self.genislik(), max(sigan, 2)
        satirlar = metin.splitlines() or ['']
        boylar = [max(1, -(-len(satir) // genislik)) for satir in satirlar]
        if sum(boylar) <= sigan:
            return metin
        secilen, kullanilan = [], 0
        for satir, boy in zip(satirlar, boylar):
            if kullanilan + boy > sigan - 1:
                break
            secilen.append(satir)
            kullanilan += boy
        if not secilen:
            secilen = [satirlar[0][:(sigan - 1) * genislik - 1] + '…']
        kalan = len(satirlar) - len(secilen)
        return '\n'.join(secilen + ([t('… ve {n} satır daha', n=kalan)] if kalan else []))

    def on_resize(self) -> None:
        if self.is_mounted:
            self.ciz()

    def action_kaydir(self, d: int) -> None:
        if self.adim == 1:
            self.secim = (self.secim + d) % len(self.secenekler)
            self.ciz()

    def action_ileri(self) -> None:
        if self.adim == 1:
            self.adim = 'ad' if self.hedef['tur'] == 'yeni' else 2
            if self.adim == 2:
                self.butce_yukle()
            self.ciz()
            self.query_one('#yg-scroll', VerticalScroll).scroll_home(animate=False)
        elif self.adim == 3:
            if self.gonderiliyor:
                return
            if self.app.ornek:
                self.app.notify(t('Önizleme: görev gönderilmedi (sahte veri).'), title='cadre')
                return
            self.gonderiliyor = True
            self.app.istek_gonder(self.request, self)

    def dolu_mu(self, metin: str) -> bool:
        if metin:
            return True
        self.app.notify(t('Görev metni boş. Önce görevi yaz.'), title='cadre', severity='warning')
        self.adim = 2
        self.ciz()
        return False

    def on_input_submitted(self, olay: Input.Submitted) -> None:
        if self.adim == 'ad' and olay.value.strip():
            self.klasor = olay.value.strip()
            self.adim = 2
            self.butce_yukle()
            self.ciz()
        elif self.adim == 1 and olay.input.id == 'yg-backlog':
            self.action_ileri()

    def butce_yukle(self) -> None:
        if self.butce_yuklendi:
            return
        from kosu import butce_ayari
        try:
            value = butce_ayari(self.app.home or arayuz_veri.ev(), 'kosu', self.hedef.get('ad', 'proje'))['deger']
            self.query_one('#yg-budget', Input).value = str(value)
        except (OSError, ValueError, KeyError, RuntimeError):
            self.query_one('#yg-budget', Input).value = ''
        self.butce_yuklendi = True

    def action_bitir(self) -> None:
        if self.adim == 1:
            self.action_ileri()
            return
        if self.adim == 2 and self.dolu_mu(self.query_one('#yg-metin', TextArea).text.strip()):
            from kosu import RequestError, validate_request
            try:
                raw = self.girdi()
                checked, _ = validate_request(self.app.home or arayuz_veri.ev(), raw)
                raw['source_token'] = checked['source_token']
            except RequestError as exc:
                self.app.notify(t(str(exc)), title=t(exc.field), severity='error')
                self.query_one('#yg-govde', Static).update(yazi(t(exc.field) + ': ' + t(str(exc)), R['kotu']))
                return
            except (OSError, ValueError):
                self.app.notify(t('Kaynak doğrulanamadı. Hedef ve dosya izinlerini kontrol et.'), title='cadre', severity='error')
                return
            self.request = raw
            self.adim = 3
            self.ciz()
            self.query_one('#yg-scroll', VerticalScroll).scroll_home(animate=False)

    def action_duzenle(self) -> None:
        if self.adim == 3 and not self.gonderiliyor:
            self.adim = 2
            self.ciz()

    def action_geri(self) -> None:
        if not self.gonderiliyor:
            self.adim = 2 if self.adim == 3 else 1
            self.request = {}
            self.ciz()

    def action_vazgec(self) -> None:
        if not self.gonderiliyor:
            self.app.pop_screen()


def bos_agac() -> dict:
    return arayuz_veri.d('cadre', '', [], [arayuz_veri.d(t(ad), id=kimlik) for kimlik, ad in BOLUMLER], id='kok')


YARDIM_TUSLARI = (
    ('← → / ↑ ↓ / Enter', 'Üstte bölüm seç. İçeride öğeyi seç ve aç.'),
    ('q / ← / Backspace', 'Bir üst menüye dön.'),
    ('1–5', 'Panel, Görevler, Bütçe, Ajanlar, Ayarlar.'),
    ('o', 'Seçilen öğenin kaydını oku.'),
    ('Shift+↑ ↓ / PgUp/PgDn', 'Ayrıntıyı kaydır.'),
    ('n', 'Yeni görev formunu aç.'),
    ('x', 'Aktif görev için iptal onayını aç.'),
    ('i', 'Aktif görevi canlı izle. q/Esc yalnız izlemeyi kapatır.'),
    ('p / m', 'Teslim adayında push / merge terminal akışını aç.'),
    ('? / Esc', 'Rehberi aç / uygulamadan çık.'),
)


def yardim_metni() -> str:
    lines = [t('Kullanım rehberi'), ''] + [f'{key:<24} {t(description)}' for key, description in YARDIM_TUSLARI]
    lines += ['', t('Okuyucu: ↑ ↓ kaydır, PgUp/PgDn/Space sayfa, q/Esc/←/o kapat. r maskeli ham kayıt.'),
        t('Onay penceresi: Enter evet, izin pencerelerinde evet/yes yazıp Enter (basılı tuş geçmez), q/Esc vazgeç, ↑ ↓ / PgUp/PgDn / End metni kaydır.'),
        t('Bütçe düzenleyici: Enter özet/kayıt, Ctrl+E düzenle, Esc vazgeç.'),
        t('Yeni görev: Tab alan, Ctrl+S devam/özet, Ctrl+B geri, Enter gönder, Esc vazgeç.'),
        t('Yeni görev kaydı plan kapısını bekler. Model veya Git işlemi başlamaz.'), '',
        t('Biten görev: Görevler › Geçmiş › görev › Sonuç / Teslim. Rapor işinde Rapor.'),
        t('Terminal onayları kesin kullanıcı izni yerine geçmez.'),
        t('Eski satır menüsü bu Textual rehberinin tuşlarını desteklemez.')]
    return '\n'.join(lines)


class Cadre(App):
    AUTO_FOCUS = None
    CSS = """
    Screen { padding: 0 2; }
    #bas { height: 1; margin-top: 1; }
    #menu { height: auto; margin-top: 1; }
    #ic { height: 1fr; margin-top: 1; }
    #sol { width: 42; height: 1fr; margin-right: 2; }
    #liste { height: 1fr; padding: 1 1; }
    #lejant { height: 4; margin-top: 1; }
    #sag { width: 1fr; height: 1fr; }
    #ust-sag { height: 1fr; }
    #orta { width: 3fr; height: 1fr; margin-right: 2; }
    #ayrinti { height: 3fr; padding: 1 2; }
    #baglam { height: 2fr; padding: 0 2; margin-top: 1; }
    #aciklama { width: 2fr; height: 1fr; padding: 1 2; }
    #alt { height: 11; margin-top: 1; }
    #canli { width: 1fr; height: 1fr; padding: 0 2; margin-right: 2; }
    #yeni { width: 30; height: 1fr; padding: 0 2; }
    #ipucu { height: 1; margin: 1 0; }
    """
    BINDINGS = [Binding('up', 'yukari', show=False), Binding('down', 'asagi', show=False), Binding('left', 'sol', show=False),
                Binding('right', 'sag', show=False), Binding('enter', 'ac', show=False), Binding('q', 'geri', show=False),
                Binding('backspace', 'geri', show=False), Binding('escape', 'quit', show=False), Binding('n', 'yeni', show=False),
                Binding('o', 'oku', show=False), Binding('x', 'iptal', show=False), Binding('i', 'izle', show=False),
                Binding('question_mark', 'yardim', show=False),
                Binding('p', 'teslim("push")', show=False), Binding('m', 'teslim("merge")', show=False),
                Binding('shift+up', 'kaydir_ayrinti(-3)', show=False), Binding('shift+down', 'kaydir_ayrinti(3)', show=False),
                Binding('pageup', 'sayfa(-1)', show=False), Binding('pagedown', 'sayfa(1)', show=False),
                *[Binding(str(i), f'bolum({i - 1})', show=False) for i in range(1, 6)]]

    def __init__(self, ornek: bool = False, home: Path | None = None, proje: str = '', benzetim: bool = True) -> None:
        super().__init__()
        palet_kur()
        self.ornek, self.varsayilan_proje, self.benzetim = ornek, proje, benzetim
        self.home = home
        self.sim = None
        if ornek:
            self.ornek_kur()
        self.kok = bos_agac()
        self.yuklendi = False
        self.yukleniyor = False
        self.bekleyen = False
        self.son_hata = ''
        self.son_olcum_istegi = 0.0
        self.yol: list[int] = []
        self.secim = 0
        self.animation_level = 'none'

    def ornek_kur(self) -> None:
        eski = self.home
        self.home, sahne = tasarim_ornek.kur()
        tasarim_ornek.sondalari_kur()
        self.sim = tasarim_ornek.Simulasyon(sahne)
        if eski is not None:
            tasarim_ornek.temizle(eski)

    def zincir(self) -> list[dict]:
        dugumler = [self.kok]
        for i in self.yol:
            dugumler.append(dugumler[-1]['cocuk'][i])
        return dugumler

    @property
    def ogeler(self) -> list[dict]:
        return self.zincir()[-1]['cocuk']

    @property
    def secili(self) -> dict:
        return self.ogeler[self.secim] if self.ogeler else arayuz_veri.d('')

    @staticmethod
    def kimlik(dugum: dict) -> str:
        return str(dugum.get('id') or dugum.get('ad'))

    def bolum_no(self) -> int:
        return self.yol[0] if self.yol else self.secim

    def bolum(self, kimlik: str) -> dict:
        return next((b for b in self.kok['cocuk'] if b.get('id') == kimlik), arayuz_veri.d(kimlik))

    def kap(self) -> dict:
        return self.zincir()[-1] if self.yol else self.kok['cocuk'][self.bolum_no()]

    def action_yukari(self) -> None:
        if self.yol and self.ogeler:
            self.secim = (self.secim - 1) % len(self.ogeler)
            self.ciz()

    def action_asagi(self) -> None:
        if not self.yol:
            self.action_ac()
        elif self.ogeler:
            self.secim = (self.secim + 1) % len(self.ogeler)
            self.ciz()

    def action_sol(self) -> None:
        if self.yol:
            self.action_geri()
        else:
            self.secim = (self.secim - 1) % len(self.kok['cocuk'])
            self.ciz()

    def action_sag(self) -> None:
        if self.yol:
            self.action_ac()
        else:
            self.secim = (self.secim + 1) % len(self.kok['cocuk'])
            self.ciz()

    def action_ac(self) -> None:
        s = self.secili
        if s.get('eylem') == 'request-authorize':
            if not self.ornek:
                self.request_approval(s['kosu'])
        elif s.get('eylem') == 'operator-approve':
            if not self.ornek:
                self.operator_approval(s['kosu'], s['token'])
        elif s.get('eylem') in ('plan-answer', 'plan-change', 'plan-stop', 'plan-withdraw', 'plan-resume', 'start', 'harcama-onay', 'request-plan'):
            if self.ornek:
                return
            if s['eylem'] in ('plan-change', 'plan-answer'):
                self.push_screen(PlanDegisikligi(self.home or arayuz_veri.ev(), s['kosu'], s['revision'], s.get('decision')))
            else:
                command = ['python3', str(MOTOR / 'lib/kosu.py'), s['eylem'], s['kosu']]
                if s.get('change_id'):
                    command.append(s['change_id'])
                if s['eylem'] == 'plan-resume':
                    command.append(str(s['revision']))
                if s['eylem'] == 'request-plan':
                    eylem = lambda: self.arkada_kos(command, t('Plan üretimi arka planda başladı. Sonuç görev durumunda görünür.'),
                                                    f"request-plan-{s['kosu']}")
                else:
                    eylem = lambda: self.kos(command, t('Plan işlemi kaydedildi.'))
                self.push_screen(Onay(s['ad'], t('Motor plan, duruş, izin ve tavan kapılarını yeniden kontrol eder.'), eylem))
        elif s.get('eylem') == 'dil':
            self.dil_degistir()
        elif s.get('eylem') == 'renk':
            self.renk_degistir()
        elif s.get('eylem') == 'testler':
            dil.testleri_yaz(not dil.testleri_oku(self.home), self.home)
            arayuz_veri.temizle()
            self.yenile(True)
        elif s.get('eylem') == 'butce-ayari':
            if self.ornek:
                self.notify(t('Önizleme: ayar kaydedilmedi (sahte veri).'), title='cadre')
                return
            self.push_screen(ButceAyari(self.home or arayuz_veri.ev(), s['butce_ayar']))
        elif s.get('eylem') == 'gorev-varsayilanlari':
            if self.ornek:
                self.notify(t('Önizleme: ayar kaydedilmedi (sahte veri).'), title='cadre')
                return
            self.push_screen(GorevVarsayilanlari(self.home or arayuz_veri.ev(), s['varsayilan_state']))
        elif s['cocuk']:
            self.yol.append(self.secim)
            self.secim = 0
            self.ciz()
        elif s.get('git'):
            self.git(s['git'])

    def action_yardim(self) -> None:
        self.push_screen(Okuyucu(t('Kullanım rehberi'), 'metin', yardim_metni()))

    def git(self, kimlikler: list[str]) -> None:
        yol, dugum = [], self.kok
        for kimlik in kimlikler:
            i = next((j for j, c in enumerate(dugum['cocuk']) if self.kimlik(c) == kimlik), None)
            if i is None:
                break
            if not dugum['cocuk'][i]['cocuk']:
                self.yol, self.secim = yol, i
                self.ciz()
                return
            yol.append(i)
            dugum = dugum['cocuk'][i]
        self.yol, self.secim = yol, 0
        self.ciz()

    def action_geri(self) -> None:
        if self.yol:
            self.secim = self.yol.pop()
            self.ciz()

    def action_bolum(self, i: int) -> None:
        self.yol, self.secim = ([i], 0) if self.kok['cocuk'][i]['cocuk'] else ([], i)
        self.ciz()

    def action_kaydir_ayrinti(self, d: int) -> None:
        self.query_one('#ayrinti', Sakin).scroll_relative(y=d, animate=False)

    def action_sayfa(self, d: int) -> None:
        kutu = self.query_one('#ayrinti', Sakin)
        (kutu.scroll_page_down if d > 0 else kutu.scroll_page_up)(animate=False)

    def action_oku(self) -> None:
        dosya = self.secili.get('dosya') if self.yol else None
        if dosya:
            baslik, tur, metin = arayuz_veri.dosya_oku(dosya)
            self.push_screen(Okuyucu(baslik, tur, metin, dosya))

    def dil_degistir(self) -> None:
        dil.yaz('en' if dil.DIL == 'tr' else 'tr')
        arayuz_veri.temizle()
        if self.ornek:
            self.ornek_kur()
        self.ciz()
        self.yenile(True)
        self.notify(t('Dil: Türkçe') if dil.DIL == 'tr' else t('Dil: English'), title='cadre')

    def renk_degistir(self) -> None:
        sira = list(dil.RENKLER)
        yeni = sira[(sira.index(R['ad']) + 1) % len(sira)] if R['ad'] in sira else sira[0]
        dil.renk_yaz(yeni)
        palet_kur(yeni)
        self.palet_uygula()
        self.ciz()
        self.yenile(True)
        self.notify(t('Renk paleti: {r}', r=yeni), title='cadre')

    def palet_uygula(self) -> None:
        self.screen.styles.background = R['arka']
        self.screen.styles.color = R['yazi']
        for w in self.screen.query('*'):
            w.styles.background = R['arka']
        for w in self.query(Sakin):
            sakin_boya(w)

    def yeni_secenekleri(self) -> list[dict]:
        home = self.home or arayuz_veri.ev()
        projeler = arayuz_veri.projeler(home)
        burasi = Path(os.getcwd()).resolve()
        bu_proje = None
        for ad in projeler:
            try:
                kayitli = Path((home / 'projects' / ad / 'repo-path').read_text().strip()).resolve()
            except OSError:
                continue
            if burasi == kayitli or ad == self.varsayilan_proje:
                bu_proje = ad
                break
        secenekler: list[dict] = []
        if bu_proje:
            secenekler.append({'tur': 'proje', 'ad': bu_proje, 'etiket': bu_proje, 'not': t('bu klasör')})
        elif burasi not in (Path.home().resolve(), Path('/')):
            secenekler.append({'tur': 'bu', 'yol': str(burasi), 'etiket': t('Bu klasör'), 'not': str(burasi)})
        secenekler += [{'tur': 'proje', 'ad': ad, 'etiket': ad, 'not': t('kayıtlı proje')} for ad in projeler if ad != bu_proje]
        secenekler.append({'tur': 'yeni', 'etiket': t('Yeni klasör oluştur'), 'not': t('Klasör ve ilk commit ayrıca izin bekler.')})
        return secenekler

    def action_yeni(self) -> None:
        self.push_screen(YeniGorev(self.yeni_secenekleri()))

    def istek_gonder(self, request: dict, screen: YeniGorev) -> None:
        def complete(result: dict, failed: bool) -> None:
            screen.gonderiliyor = False
            if failed:
                message = t(result['message'])
                self.notify(message, title=t(result['error']), severity='error')
                if screen.is_mounted:
                    screen.query_one('#yg-govde', Static).update(yazi(t(result['error']) + ': ' + message, R['kotu']))
                return
            if screen.is_mounted and self.screen is screen:
                self.pop_screen()
            message = t('Görev kaydedildi: {r} · plan kapısı bekleniyor.', r=result['run_id']) if result['status'] == 'waiting' else \
                t('Mevcut görev: {r} · {s}', r=result['run_id'], s=t(arayuz_veri.DURUM_ADI.get(result['status'], result['status'])))
            self.notify(message, title='cadre')
            self.yenile(True)

        def send() -> None:
            home = self.home or arayuz_veri.ev()
            try:
                result = subprocess.run(['python3', str(MOTOR / 'lib/kosu.py'), 'request'], input=json.dumps(request),
                                        text=True, capture_output=True, timeout=30,
                                        env={**os.environ, 'CADRE_HOME': str(home), 'CADRE_ENGINE_ROOT': str(MOTOR)})
                data = json.loads(result.stderr if result.returncode else result.stdout)
                failed = result.returncode != 0
            except (OSError, subprocess.SubprocessError, ValueError):
                data, failed = {'error': 'request', 'message': 'Görev kaydedilemedi. Aynı gönderimi yeniden deneyebilirsin.'}, True
            self.call_from_thread(complete, data, failed)
        self.run_worker(send, thread=True, group='istek', exit_on_error=False)

    def gorev_gonder(self, hedef: dict, metin: str) -> None:
        if self.ornek:
            self.notify(t('Önizleme: görev gönderilmedi (sahte veri).'), title='cadre')
            return
        self.notify(t('Görev gönderiliyor…'), title='cadre')
        self.run_worker(lambda: self.gonder(hedef, metin), thread=True, group='gonder', exit_on_error=False)

    def gonder(self, hedef: dict, metin: str) -> None:
        import proje_kayit
        try:
            mesaj, hata = self.gorev_kaydet(hedef, metin)
        except proje_kayit.KayitIzni as exc:
            self.call_from_thread(self.operator_approval, 'system', exc.token,
                                  lambda: self.gorev_gonder({**hedef, 'tur': 'bu'}, metin))
            return
        self.call_from_thread(self.notify, mesaj, title='cadre', severity='error' if hata else 'information')
        self.call_from_thread(self.yenile, True)

    def gorev_kaydet(self, hedef: dict, metin: str) -> tuple[str, bool]:
        import proje_kayit
        home = self.home or arayuz_veri.ev()
        ad = hedef.get('ad')
        try:
            if hedef.get('tur') in ('bu', 'yeni'):
                ad = proje_kayit.kaydet(hedef['yol'], olustur=hedef['tur'] == 'yeni', home=home)
        except proje_kayit.KayitIzni:
            raise
        except (proje_kayit.KayitHatasi, OSError, subprocess.SubprocessError) as exc:
            return str(exc), True
        try:
            repo = (home / 'projects' / str(ad) / 'repo-path').read_text().strip()
        except OSError:
            return t('{p} projesinin repo yolu okunamadı.', p=ad), True
        try:
            r = subprocess.run(['bash', str(MOTOR / 'bin/cadre.sh'), 'gorev', metin], cwd=repo, capture_output=True, text=True, timeout=120,
                               env={**os.environ, 'CADRE_IZLE': '0', 'CADRE_ENGINE_ROOT': str(MOTOR), 'CADRE_HOME': str(home)})
        except (OSError, subprocess.SubprocessError) as exc:
            return t('Komut yanıt vermedi: {e}', e=arayuz_veri.hata_yazi(exc)), True
        sonuc = (r.stdout + r.stderr).strip().splitlines()
        return next((s for s in sonuc if 'kaydedildi' in s or 'SIRAYA' in s), sonuc[-1] if sonuc else t('gönderildi')), r.returncode != 0

    def action_iptal(self) -> None:
        s = self.secili
        if not s.get('aktif'):
            return
        if self.ornek:
            self.notify(t('Önizleme: iptal edilmedi (sahte veri).'), title='cadre')
            return
        self.push_screen(Onay(t('{a} görevini iptal edeyim mi?', a=s['ad']), t('Çalışan adım durdurulur, yarım ürün korunur.'),
                              lambda: self.kos(['python3', str(MOTOR / 'lib/kosu.py'), 'cancel', s['kosu']], t('iptal istendi'))))

    def kos(self, komut: list[str], mesaj: str, zaman: int = 120) -> None:
        try:
            r = subprocess.run(komut, capture_output=True, text=True, timeout=zaman,
                               env={**os.environ, 'CADRE_ENGINE_ROOT': str(MOTOR), 'CADRE_HOME': str(self.home or arayuz_veri.ev())})
        except (OSError, subprocess.SubprocessError) as exc:
            self.notify(t('Komut yanıt vermedi: {e}', e=arayuz_veri.hata_yazi(exc)), title='cadre', severity='error')
        else:
            self.notify(mesaj if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:], title='cadre',
                        severity='information' if r.returncode == 0 else 'error')
        self.yenile(True)

    def arkada_kos(self, komut: list[str], mesaj: str, ad: str) -> None:
        home = Path(self.home or arayuz_veri.ev())
        try:
            arkada_baslat(komut, {**os.environ, 'CADRE_ENGINE_ROOT': str(MOTOR), 'CADRE_HOME': str(home)}, home / 'logs' / f'{ad}.log')
        except OSError as exc:
            self.notify(t('Komut yanıt vermedi: {e}', e=arayuz_veri.hata_yazi(exc)), title='cadre', severity='error')
        else:
            self.notify(mesaj, title='cadre')
        self.yenile(True)

    def operator_approval(self, rid: str, token: str, after=None) -> None:
        from izin import approve, read_action
        from kosu import RunStore
        store = RunStore(self.home or arayuz_veri.ev())
        try:
            action = read_action(store, rid, token)
        except (OSError, ValueError, KeyError) as exc:
            self.notify(arayuz_veri.hata_yazi(exc), title='cadre', severity='error')
            return

        def record(confirmation: str = '') -> None:
            try:
                receipt = approve(store, rid, token, confirmation)
            except (OSError, ValueError, KeyError) as exc:
                self.notify(arayuz_veri.hata_yazi(exc), title='cadre', severity='error')
                return
            if receipt['status'] == 'first-approved':
                self.push_screen(Onay(t('İkinci kesin onay'), receipt['display'],
                                      lambda: record(receipt['confirmation']), yazili=True))
            else:
                try:
                    if receipt['identity']['kind'] == 'init-commit':
                        from izin import initialization_execute
                        initialization_execute(store, token)
                    elif receipt['identity']['kind'] == 'model-calls':
                        from izin import model_activate
                        model_activate(store, rid, token)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    self.notify(arayuz_veri.hata_yazi(exc), title='cadre', severity='error')
                    self.yenile(True)
                    return
                self.yenile(True)
                if after is not None:
                    after()
        if action['status'] == 'first-approved':
            self.push_screen(Onay(t('İkinci kesin onay'), action['display'], lambda: record(action['confirmation']), yazili=True))
        elif action['status'] == 'pending':
            self.push_screen(Onay(t('Kesin eylem izni'), action['identity']['kind'] + '\n' + action['display'], record, yazili=True))

    def request_approval(self, rid: str) -> None:
        base = ['python3', str(MOTOR / 'lib/kosu.py')]
        env = {**os.environ, 'CADRE_ENGINE_ROOT': str(MOTOR), 'CADRE_HOME': str(self.home or arayuz_veri.ev())}
        try:
            response = subprocess.run([*base, 'request-proposal', rid], capture_output=True, text=True, env=env, timeout=15)
            if response.returncode != 0:
                self.notify((response.stderr or response.stdout).strip()[-200:] or t('İstek önerisi alınamadı.'), title='cadre', severity='error')
                return
            proposal = json.loads(response.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.notify(arayuz_veri.hata_yazi(exc), title='cadre', severity='error')
            return
        detail = arayuz_veri.istek_onay_metni(rid, proposal)

        def approve(confirmation: str = '') -> None:
            command = [*base, 'request-approve', rid, *([confirmation] if confirmation else [])]
            try:
                result = subprocess.run(command, input=json.dumps(proposal), capture_output=True, text=True, env=env, timeout=15)
                if result.returncode != 0:
                    self.notify((result.stderr or result.stdout).strip()[-200:] or t('İstek onayı kaydedilmedi.'), title='cadre', severity='error')
                    return
                receipt = json.loads(result.stdout)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.notify(arayuz_veri.hata_yazi(exc), title='cadre', severity='error')
                return
            if receipt['status'] == 'first-approved':
                exact = ("printf '%s' " + shlex.quote(json.dumps(proposal, ensure_ascii=False)) + ' | ' +
                         shlex.join([*base, 'request-approve', rid, receipt['confirmation']]))
                self.push_screen(Onay(t('İkinci kesin onay'), arayuz_veri.istek_onay_metni(rid, proposal, exact),
                                      lambda: approve(receipt['confirmation']), yazili=True))
            else:
                self.yenile(True)
        self.push_screen(Onay(t('Göreve özel çağrı izni'), detail, approve, yazili=True))

    def action_izle(self) -> None:
        s = self.secili
        if not s.get('aktif') or not s.get('proje'):
            return
        if self.ornek:
            self.notify(t('Önizleme: canlı izleme yalnız gerçek görevde açılır.'), title='cadre')
            return
        self.push_screen(CanliIzleme(self.home or arayuz_veri.ev(), str(s['kosu'])))

    def action_teslim(self, tur: str) -> None:
        s = self.secili
        if not s.get('teslim'):
            return
        if self.ornek:
            self.notify(t('Önizleme: {t} yapılmadı (sahte veri).', t=tur), title='cadre')
            return
        with self.suspend():
            try:
                subprocess.run(['bash', str(MOTOR / 'bin/cadre.sh'), tur, s['proje'], s['kosu']])
            except OSError as exc:
                print(arayuz_veri.hata_yazi(exc))
            input(t('\nArayüze dönmek için Enter… '))
        self.yenile(True)

    def isaret(self, dugum: dict) -> Text:
        durum = dugum.get('durum') or ''
        return Text(f"{ISARET.get(durum, '·')} ", style=renk(durum))

    def menu_r(self) -> Table:
        izgara = Table.grid(expand=True, padding=(0, 1))
        for _ in self.kok['cocuk']:
            izgara.add_column(ratio=1)
        kutular = []
        for i, b in enumerate(self.kok['cocuk']):
            secili = i == self.bolum_no()
            kutular.append(Panel(Text(b['ad'], style=f"bold {R['vurgu']}" if secili else R['soluk'], justify='center'), box=box.ROUNDED,
                                 height=3, style=f"on {R['yuzey']}" if secili else '', border_style=R['vurgu'] if secili else R['cizgi']))
        izgara.add_row(*kutular)
        return izgara

    def liste_r(self, ogeler: list[dict], secim: int | None, yukseklik: int) -> Group:
        if not ogeler:
            return Group(yazi(''), yazi(t('burada bir şey yok') if self.yuklendi else '', R['soluk']))
        sigan = max(1, (yukseklik - 2) // SATIR_BASINA)
        bas = 0 if secim is None else min(max(0, secim - sigan // 2), max(0, len(ogeler) - sigan))
        satirlar = Table.grid(expand=True, padding=0)
        satirlar.add_column(ratio=1)
        satirlar.add_row(yazi(t('  ▲ {n} tane yukarıda', n=bas), R['soluk']) if bas else yazi(''))
        for i, c in enumerate(ogeler[bas:bas + sigan], bas):
            secili = i == secim
            arka = f" on {R['yuzey']}" if secili else ''
            ust = Text('› ' if secili else '  ', style=R['vurgu'] + arka)
            ust.append_text(self.isaret(c))
            ust.append(c['ad'], style=(f"bold {R['vurgu']}" if secili else (R['yazi'] if secim is not None else R['soluk'])) + arka)
            if c['cocuk']:
                ust.append('  ›', style=R['soluk'] + arka)
            elif c.get('git') or c.get('eylem'):
                ust.append('  →', style=R['soluk'] + arka)
            ozet = f"{c['durum_yazi']} · {arayuz_veri.goreli_yas(c.get('created'))}" if 'yas' in c else c['ozet']
            proje = f"     {c.get('proje') or '?'}" if 'yas' in c else ''
            for satir in (tek_satir(ust), tek_satir(yazi(f"     {ozet}" if ozet else '', R['soluk'])), tek_satir(yazi(proje, R['soluk']))):
                satirlar.add_row(satir, style=f"on {R['yuzey']}" if secili else '')
        kalan = len(ogeler) - bas - sigan
        if kalan > 0:
            satirlar.add_row(yazi(t('  ▼ {n} tane aşağıda', n=kalan), R['soluk']))
        return Group(satirlar)

    def ayrinti_r(self, dugum: dict, ust_duzey: bool) -> Group:
        if dugum.get('kompakt'):
            return Group(*form(dugum['ayrinti']))
        onizleme = bool(dugum['cocuk']) and not ust_duzey
        satir, alan = tahmini_satir(dugum['ayrinti'])
        satir += (min(len(dugum['cocuk']), 6) + 4) if onizleme else 3
        yukseklik = (self.query_one('#ayrinti').size.height or 30) if self.is_mounted else 30
        parcalar = form(dugum['ayrinti'], ferah=satir + alan <= yukseklik)
        ipuclari = []
        if ust_duzey:
            if dugum['cocuk']:
                ipuclari.append(t('↓ Enter  {b} içine gir', b=dugum['ad']))
        elif onizleme:
            parcalar.append(bolum_cizgisi(t('İçinde · {n}', n=len(dugum['cocuk']))))
            for c in dugum['cocuk'][:6]:
                satir_ = Text('  ')
                satir_.append_text(self.isaret(c))
                satir_.append(c['ad'], style=R['yazi'])
                if c['ozet']:
                    satir_.append(f"   {c['ozet']}", style=R['soluk'])
                parcalar.append(tek_satir(satir_))
            if len(dugum['cocuk']) > 6:
                parcalar.append(yazi(t('  … ve {n} tane daha', n=len(dugum['cocuk']) - 6), R['soluk']))
            ipuclari.append(t('→ Enter  içine gir'))
        elif dugum.get('git'):
            ipuclari.append(t('→ Enter  {y}', y=dugum.get('git_yazi') or ''))
        elif dugum.get('eylem') == 'dil':
            ipuclari.append(t('→ Enter  dili değiştir'))
        elif dugum.get('eylem') == 'renk':
            ipuclari.append(t('→ Enter  sıradaki palet'))
        if dugum.get('dosya') and not ust_duzey:
            ipuclari.append(t('o  {b} oku', b=dugum['dosya'].get('baslik') or ''))
        if ipuclari:
            parcalar += [yazi('')] + [yazi(i, R['vurgu']) for i in ipuclari]
        return Group(*parcalar)

    def aciklama_r(self) -> Group:
        kap = self.kap()
        bloklar = kap.get('aciklama') or ([('baslik', kap['ad'])] + ([('bolum', t('İçinde'))] + [('alan', (c['ad'], c['ozet'])) for c in kap['cocuk']]
                                                                     if kap['cocuk'] else []))
        return Group(*form(bloklar, ferah=True))

    def compose(self) -> ComposeResult:
        yield Static(id='bas')
        yield Static(id='menu')
        with Horizontal(id='ic'):
            with Vertical(id='sol'):
                yield Static(id='liste')
                yield Static(id='lejant')
            with Vertical(id='sag'):
                with Horizontal(id='ust-sag'):
                    with Vertical(id='orta'):
                        yield Sakin(Static(id='ayrinti-metin'), id='ayrinti')
                        yield Sakin(Static(id='baglam-metin'), id='baglam')
                    yield Sakin(Static(id='aciklama-metin'), id='aciklama')
                with Horizontal(id='alt'):
                    yield Static(id='canli')
                    yield Static(id='yeni')
        yield Static(id='ipucu')

    def on_mount(self) -> None:
        self.palet_uygula()
        self.ciz()
        self.set_interval(1, self.tik)
        self.yenile()
        self.set_interval(2, self.yenile)
        if self.ornek and self.benzetim:
            self.set_interval(4, self.benzet)

    def on_unmount(self) -> None:
        if self.ornek:
            tasarim_ornek.sondalari_kaldir()
            if self.home is not None:
                tasarim_ornek.temizle(self.home)

    def benzet(self) -> None:
        if self.sim is not None:
            self.sim.adim()

    def yenile(self, zorla: bool = False) -> None:
        if self.yukleniyor:
            self.bekleyen = self.bekleyen or zorla
            return
        self.yukleniyor, self.bekleyen = True, False
        self.run_worker(self.yukle, thread=True, group='veri', exit_on_error=False)

    def olcum_tazele(self) -> None:
        if self.ornek or os.environ.get('CADRE_AG_YOK') == '1' or time.time() - self.son_olcum_istegi < 60:
            return
        home = self.home or arayuz_veri.ev()
        if arayuz_veri.butce_gorunumu(home)[1] >= 60:
            import ekran
            self.son_olcum_istegi = time.time()
            ekran.arkada_tazele(home)

    def yukle(self) -> None:
        try:
            self.olcum_tazele()
            self.call_from_thread(self.kok_degistir, arayuz_veri.agac(self.home, ajanlar=False))
            self.call_from_thread(self.kok_degistir, arayuz_veri.agac(self.home, ajanlar=True))
        except Exception as exc:
            self.call_from_thread(self.yukleme_hatasi, exc)
        finally:
            self.yukleniyor = False
            if self.bekleyen:
                self.call_from_thread(self.yenile)

    def yukleme_hatasi(self, exc: Exception) -> None:
        metin = arayuz_veri.hata_yazi(exc)
        if metin != self.son_hata:
            self.son_hata = metin
            self.notify(t('Veriler okunamadı: {e}', e=metin), title='cadre', severity='error')

    def kok_degistir(self, kok: dict) -> None:
        kimlikler = [self.kimlik(d) for d in self.zincir()[1:]]
        secili = self.kimlik(self.secili)
        self.kok, self.yol, dugum, self.yuklendi = kok, [], kok, True
        for kimlik in kimlikler:
            i = next((j for j, c in enumerate(dugum['cocuk']) if self.kimlik(c) == kimlik), None)
            if i is None or not dugum['cocuk'][i]['cocuk']:
                break
            self.yol.append(i)
            dugum = dugum['cocuk'][i]
        self.secim = next((j for j, c in enumerate(dugum['cocuk']) if self.kimlik(c) == secili), min(self.secim, max(len(dugum['cocuk']) - 1, 0)))
        self.ciz()

    def tik(self) -> None:
        if not self.query('#bas'):
            return
        self.query_one('#bas', Static).update(self.bas_r())
        self.query_one('#canli', Static).update(self.canli_r())
        self.query_one('#ipucu', Static).update(self.ipucu_r())

    def aktif_isler(self) -> list[dict]:
        return [j for j in self.bolum('gorevler').get('cocuk', []) if j.get('aktif')]

    def bas_r(self) -> Text:
        pending = sum(bool(j.get('girdi_bekliyor')) for j in self.aktif_isler())
        waiting = sum(bool(j.get('beklemede')) and not j.get('girdi_bekliyor') for j in self.aktif_isler())
        parcalar = ['cadre', t('{n} görev sürüyor', n=len(self.aktif_isler()) - pending - waiting) if self.yuklendi else '',
                    t('{n} görev bekliyor', n=waiting) if waiting else '',
                    t('{n} girdi plan bekliyor', n=pending) if pending else '', self.bolum('butce').get('ozet', ''),
                    t('önizleme (sahte veri)') if self.ornek else t('canlı'), time.strftime('%H:%M:%S')]
        return tek_satir(yazi('   ·   '.join(p for p in parcalar if p), R['soluk']))

    def canli_r(self) -> Group:
        aktif = self.aktif_isler()
        if not aktif:
            return Group(yazi(t('Şu an çalışan görev yok.'), R['soluk']), yazi(t('Yeni görev için n.'), R['soluk']))
        sigan = max(1, ((self.query_one('#canli').size.height or 9) + 1) // 3)
        goster = aktif if len(aktif) <= sigan else aktif[:max(sigan - 1, 1)]
        satirlar: list = []
        for sira, j in enumerate(goster):
            if sira:
                satirlar.append(yazi(''))
            def bicim(sure: int) -> str:
                return (t('{s} sa {d:02d} dk', s=sure // 3600, d=sure % 3600 // 60) if sure >= 3600
                        else t('{d} dk {sn:02d} sn', d=sure // 60, sn=sure % 60))
            saat = j.get('saat')
            sure_yazi = (t('çalışma {c} · bekleme {b}', c=bicim(int(saat['calisma_sn'])), b=bicim(int(saat['bekleme_sn'])))
                         if isinstance(saat, dict) else bicim(int(time.time() - float(j.get('created') or time.time()))))
            waiting = j.get('girdi_bekliyor') or j.get('beklemede')
            satirlar.append(tek_satir(Text.assemble(('○ ' if waiting else '◉ ', R['soluk'] if waiting else R['vurgu']),
                                                   (j['ad'], f"bold {R['yazi']}"), yazi(f'   {sure_yazi}', R['soluk']))))
            alt = Text('  ')
            alt.append(str(j.get('asama') or ''), style=R['yazi'])
            alt.append('   ')
            alt.append(str(j.get('su_an') or j.get('ozet') or ''), style=R['soluk'])
            satirlar.append(tek_satir(alt))
        if len(aktif) > len(goster):
            satirlar.append(yazi(t('… ve {n} görev daha · Görevler', n=len(aktif) - len(goster)), R['soluk']))
        return Group(*satirlar)

    def baglam_r(self) -> tuple[Group | None, str]:
        adaylar = [self.secili] + list(reversed(self.zincir()[1:])) if self.yol else [self.secili]
        kaynak = next((d['baglam'] for d in adaylar if d.get('baglam')), None)
        if not kaynak:
            return None, t('Bağlam')
        baslik = kaynak[0][1] if kaynak[0][0] == 'baslik' else t('Bağlam')
        bloklar = [('bolum', v) if tur == 'baslik' else (tur, v) for tur, v in (kaynak[1:] if kaynak[0][0] == 'baslik' else kaynak)]
        return Group(*form(bloklar)), baslik

    def kutu(self, kimlik: str, baslik: str, etkin: bool = False) -> None:
        w = self.query_one(kimlik)
        renk_ = R['vurgu'] if etkin else R['cizgi']
        w.styles.border = ('round', renk_)
        w.styles.border_title_color = R['vurgu'] if etkin else R['soluk']
        w.styles.background = R['arka']
        w.border_title = baslik

    def iz(self) -> str:
        adlar = [d['ad'] for d in self.zincir()[1:]]
        metin = '  ›  '.join(adlar)
        while len(metin) > 36 and len(adlar) > 1:
            adlar.pop(0)
            metin = '…  ›  ' + '  ›  '.join(adlar)
        return metin if len(metin) <= 36 else metin[:35] + '…'

    def ipucu_r(self) -> Text:
        s = self.secili
        if not self.yol:
            parcalar = [t('?  rehber'), t('← →  bölüm'), t('↓ Enter  içine gir'), t('1–5  bölüm'), t('n  yeni görev'), t('Esc  çık')]
        else:
            kayar = self.query_one('#ayrinti', Sakin).max_scroll_y > 0
            ac = t('→ Enter  değiştir') if s.get('eylem') else (t('→ Enter  aç') if s['cocuk'] or s.get('git') else '')
            parcalar = [t('?  rehber'), t('↑ ↓  seç'), ac, t('← q  geri'), t('o  oku') if s.get('dosya') else '',
                        t('Shift+↑ ↓  ayrıntı') if kayar else '', t('n  yeni görev')]
            if s.get('aktif'):
                parcalar += [t('x  iptal'), t('i  canlı izle')]
            if s.get('teslim'):
                parcalar += [t('p  push'), t('m  merge')]
            parcalar.append(t('Esc  çık'))
        return tek_satir(yazi('      '.join(p for p in parcalar if p), R['soluk']))

    def ciz(self) -> None:
        if not self.query('#bas'):
            return
        while self.yol and not self.ogeler:
            self.secim = self.yol.pop()
        self.secim = min(self.secim, max(len(self.ogeler) - 1, 0))
        bolum = self.kok['cocuk'][self.bolum_no()]
        compact = not self.yol and bolum.get('kompakt', False)
        self.query_one('#alt').display = not compact
        self.query_one('#bas', Static).update(self.bas_r())
        menu = self.query_one('#menu', Static)
        menu.display = not self.yol
        menu.update(self.menu_r())
        yukseklik = max(6, self.query_one('#liste').size.height or 30)
        if self.yol:
            liste, baslik = self.liste_r(self.ogeler, self.secim, yukseklik), self.iz()
        else:
            liste, baslik = self.liste_r(bolum['cocuk'], None, yukseklik), t('{b}  ·  Enter ile gir', b=bolum['ad'])
        gizli = self.bolum('gorevler').get('gizli_test', 0)
        if gizli and self.yol == [1]:
            baslik += ' · ' + t('{n} test görevi gizli', n=gizli)
        self.kutu('#liste', baslik, etkin=bool(self.yol))
        self.query_one('#liste', Static).update(liste)
        self.query_one('#lejant', Static).update(Group(
            Text.assemble(('● ', R['iyi']), (t('tamam') + '       ', R['soluk']), ('◉ ', R['vurgu']), (t('sürüyor'), R['soluk'])),
            Text.assemble(('○ ', R['soluk']), (t('sırada') + '      ', R['soluk']), ('✗ ', R['kotu']), (t('sorun'), R['soluk'])),
            Text.assemble(('›  ', R['soluk']), (t('içi var, Enter ile girilir'), R['soluk'])),
            Text.assemble(('→  ', R['soluk']), (t('Enter ilgili yere götürür'), R['soluk']))))
        self.kutu('#ayrinti', t('Ayrıntı'))
        self.query_one('#ayrinti-metin', Static).update(self.ayrinti_r(self.secili, not self.yol))
        baglam, b_baslik = self.baglam_r()
        self.query_one('#baglam').display = baglam is not None and not compact
        if baglam is not None:
            self.kutu('#baglam', b_baslik)
            self.query_one('#baglam-metin', Static).update(baglam)
        self.kutu('#aciklama', t('Bu menü'))
        self.query_one('#aciklama-metin', Static).update(self.aciklama_r())
        k = min(max(len(self.aktif_isler()), 1), 3 if self.size.height >= 36 else 1)
        self.query_one('#alt').styles.height = 2 + 3 * k
        self.kutu('#canli', t('Açık görevler') if any(j.get('girdi_bekliyor') or j.get('beklemede') for j in self.aktif_isler()) else t('Şu an çalışan'))
        self.query_one('#canli', Static).update(self.canli_r())
        self.kutu('#yeni', t('Yeni görev'), etkin=True)
        self.query_one('#yeni', Static).update(Group(Text.assemble(('n', f"bold {R['vurgu']}"), ('   ' + t('yeni görev ver'), f"bold {R['yazi']}")),
                                                     yazi(t('istek ve sınırları kaydet'), R['soluk']), yazi(t('plan kapısını bekler'), R['soluk'])))
        self.query_one('#ipucu', Static).update(self.ipucu_r())

    def on_resize(self, olay) -> None:
        genis = olay.size.width >= 130
        self.query_one('#aciklama').display = genis
        self.query_one('#yeni').display = genis
        self.query_one('#sol').styles.width = 42 if genis else 36
        self.query_one('#orta').styles.margin = (0, 2 if genis else 0, 0, 0)
        self.query_one('#canli').styles.margin = (0, 2 if genis else 0, 0, 0)
        self.call_after_refresh(self.ciz)


if __name__ == '__main__':
    dil.kur()
    palet_kur()
    Cadre(ornek='--ornek' in sys.argv[1:], proje=os.environ.get('CADRE_PROJE', '')).run()
