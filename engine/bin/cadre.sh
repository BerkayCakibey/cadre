#!/usr/bin/env bash
# cadre — ONE COMMAND. Menu: 1 task · 2 watch · 3 status · 4 cancel · 5 push · 6 merge · 7 ledger · 8 summary · 9 balance.
# Usage: `cadre [gorev|izle|durum|iptal|push|merge|devam ID]`
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
C=$'\033[38;5;44m'; D=$'\033[38;5;245m'; G=$'\033[38;5;42m'; Y=$'\033[38;5;220m'; RD=$'\033[38;5;203m'; B=$'\033[1m'; R=$'\033[0m'
{ [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; } && C='' D='' G='' Y='' RD='' B='' R=''

root="$(qe_repo_root)"; project=""
[ -n "$root" ] && project="$(qe_project_for_dir "$root" 2>/dev/null || true)"

calisan_var(){ pgrep -f "qe_core.py motor ${1:-}|cadre-dispatch.sh ${1:-}|orkestra.sh ${1:-}" >/dev/null 2>&1; }
# P2: every time `cadre` is called, dead guards are revived from the code root (one command brings everything up; a live pidfile is left alone)
bekciler_ayakta(){ python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" bekci-dirilt >/dev/null 2>&1 || true; }
son_log(){
  local latest
  latest="$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" latest-log "$1")" || return 1
  if [ -n "$latest" ]; then printf '%s\n' "$latest"; else ls -t "$CADRE_LOGS"/is-"$1"-*.log 2>/dev/null | head -1; fi
}
son_defter(){ ls -td "$CADRE_HOME"/reports/kosu-"$1"-* 2>/dev/null | head -1; }

simge(){ python3 "$CADRE_ENGINE_ROOT/lib/ekran.py" simge "$1"; }
baslik(){ printf '\n%s%scadre · %s%s\n\n' "$C" "$B" "${project:-projeler}" "$R"; }

# Item 0a: a stuck reservation / price failure silently locked the budget; the status screen now says so.
kurtarma_uyari(){
  python3 - "$CADRE_HOME/zincir/request-budget.db" <<'PY' 2>/dev/null
import sqlite3, sys, time
try:
    con = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
    bayat, tutar = con.execute('select count(*),coalesce(sum(reserved),0) from reservation '
                               'where actual is null and created < ?', (time.time() - 6 * 3600,)).fetchone()
    try:
        ariza = [r[0] for r in con.execute('select provider from pricing_fault')]
    except sqlite3.Error:
        ariza = []
except sqlite3.Error:
    raise SystemExit(0)
if bayat:
    print('%d askıda rezervasyon ($%.2f) 6 saatten eski → cadre kurtar --bayat-listele' % (bayat, tutar / 1e6))
if ariza:
    print('fiyat arızası, sağlayıcı kapalı: %s → cadre kurtar --ariza-listele' % ', '.join(ariza))
PY
}

# 0c: the Zen balance CANNOT BE READ by a program. The user enters it, and we subtract the spending we measured ourselves.
# `cadre zen-bakiye` -> 1 query, 2 top up. CADRE_ZEN_SECIM / CADRE_ZEN_TUTAR drive it from tests.
zen_dosya(){ printf '%s\n' "$CADRE_HOME/zincir/zen-bakiye.txt"; }
durum(){
  bekciler_ayakta
  python3 "$CADRE_ENGINE_ROOT/lib/ekran.py" durum "${1:-$project}"
}

zen_durum(){
  python3 "$CADRE_ENGINE_ROOT/lib/butce.py" 2>/dev/null | python3 -c '
import json,sys
try: z=json.load(sys.stdin).get("zen") or {}
except Exception: z={}
if not z or z.get("db")=="yok":
    print("kayit yok — once `cadre zen-bakiye` menusunden 2 ile bakiye gir"); raise SystemExit(0)
print("girilen   : $%.2f  (%s gunu girildi)" % (z["bakiye_giris"], z["giris_tarihi"]))
print("harcanan  : $%.4f  (o tarihten beri, kendi olcumumuz)" % z["harcanan"])
print("kalan     : $%.2f" % z["kalan"])
print("kullanilabilir: $%.3f" % z["kullanilabilir"])
if z.get("db")!="ok": print("UYARI: olcum durumu " + str(z.get("db")))
'
}
zen_yukle(){
  local f tutar kalan yeni
  f="$(zen_dosya)"
  kalan="$(python3 "$CADRE_ENGINE_ROOT/lib/butce.py" 2>/dev/null | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("zen") or {}).get("kalan") or 0)
except Exception: print(0)')"
  printf 'Şu anki kalan (bizim hesabımız): $%s\n' "$kalan"
  if [ -n "${CADRE_ZEN_TUTAR:-}" ]; then tutar="$CADRE_ZEN_TUTAR"; else
    printf 'Yüklediğin tutar (USD, sadece sayı; boş = vazgeç): '; read -r tutar || tutar=""
  fi
  [[ "$tutar" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "geçersiz tutar, hiçbir şey yazılmadı" >&2; return 1; }
  yeni="$(python3 -c "print(f'{float('$kalan')+float('$tutar'):.2f}')" 2>/dev/null)" || yeni=""
  [ -n "$yeni" ] || { echo "bakiye hesaplanamadı, hiçbir şey yazılmadı" >&2; return 1; }
  printf 'Yeni bakiye: $%s  — bu tarihten sonraki harcama bundan düşülecek.\n' "$yeni"
  mkdir -p "$(dirname "$f")"
  # B92: the baseline must be the ENTRY TIME. Without a timestamp butce.py also subtracted what was spent EARLIER that day
  # and entering a fresh balance SHRANK the share (measured: $9.96 entered, the system said $2.34).
  printf '%sT%s %s\n' "$(date +%Y-%m-%d)" "$(date +%H:%M:%S)" "$yeni" > "$f.yeni" && mv -f "$f.yeni" "$f" || { echo "bakiye yazılamadı" >&2; return 1; }
  printf 'yazıldı: %s\n' "$f"
}
bakiye_menu(){
  python3 "$CADRE_ENGINE_ROOT/lib/opencode_butce.py"
  local s
  if [ -n "${CADRE_BAKIYE_SECIM:-}" ]; then s="$CADRE_BAKIYE_SECIM"; else
    printf '\n%sBakiye%s\n  1) zen: sorgula\n  2) zen: yükle\nSeçim: ' "$B" "$R"
    read -r s || s=""
  fi
  case "$s" in
    1|"") zen_durum ;;
    2)    zen_yukle ;;
    *)    echo "bilinmeyen seçim" >&2; return 2 ;;
  esac
}

zen_bakiye(){
  local s
  if [ -n "${CADRE_ZEN_SECIM:-}" ]; then s="$CADRE_ZEN_SECIM"; else
    printf '\n%szen bakiye%s\n  1) sorgula\n  2) yükle\nSeçim: ' "$B" "$R"; read -r s || s=""
  fi
  case "$s" in
    1|"") zen_durum ;;
    2)    zen_yukle ;;
    *)    echo "bilinmeyen seçim" >&2; return 2 ;;
  esac
}

durum_ayrinti(){
  local p="${1:-$project}"
  bekciler_ayakta
  printf '%s── DURUM ──────────────────────────────────────%s\n' "$D" "$R"
  if [ -n "$p" ]; then printf '  proje    : %s%s%s  (%s)\n' "$B" "$p" "$R" "$(qe_repo_path "$p")"
    printf '  aktif dal: %s\n' "$(git -C "$(qe_repo_path "$p")" branch --show-current 2>/dev/null)"
  else printf '  proje    : %s(bu dizin kayıtlı bir proje değil)%s\n' "$Y" "$R"; fi
  local n; n=$(pgrep -f 'qe_core.py motor' | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then printf '  iş       : %s%s motor çalışıyor%s — %s\n' "$G" "$n" "$R" "$(pgrep -fl 'qe_core.py motor' | awk '{print $NF}' | sort -u | tr '\n' ' ')"
  elif [ -n "$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" busy 2>/dev/null)" ]; then printf '  iş       : %skoşu çalışıyor%s (ayrıntı: koşu satırları)\n' "$G" "$R"
  else printf '  iş       : %syok%s\n' "$D" "$R"; fi
  # P2: liveness = pidfile + argv + heartbeat (kosu.py bekci-canli); P1: helpers from the code root
  printf '  bekçiler : saglik-nobet %s · kota-izle %s\n' "$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" bekci-canli saglik-nobet >/dev/null 2>&1 && printf '%sOK%s' "$G" "$R" || printf '%sYOK%s' "$RD" "$R")" "$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" bekci-canli kota-izle >/dev/null 2>&1 && printf '%sOK%s' "$G" "$R" || printf '%sYOK%s' "$RD" "$R")"
  if [ -n "$p" ]; then local tb; tb="$(teslim_adaylari "$p" 2>/dev/null | grep -c . || true)"
    if [ "${tb:-0}" -gt 0 ]; then
      printf '  teslim   : %s%s bitmiş iş onay bekliyor%s  → menü 6 push · 7 merge\n' "$Y" "$tb" "$R"
      # finding: every run branches FROM THE MAIN BRANCH. Undelivered work does not accumulate; a new job does not see
      # the old fixes and QA may turn red because of that. The user should not hand out work without knowing this.
      printf '             %syeni iş verirsen bunlar birikmez — her koşu ana daldan başlar%s\n' "$D" "$R"
    fi; fi
  local bj; bj="$(python3 "$CADRE_ENGINE_ROOT/lib/butce.py" 2>/dev/null)"
  [ -n "$bj" ] && printf '%s' "$bj" | python3 -c 'import json,sys,os;sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib");from ekran import butce_ozeti;print("  bütçe    : "+butce_ozeti(json.load(sys.stdin)))'

  # Item 5: three windows on the status screen; visible before hitting the wall (from cache, no network)
  local pj; pj="$(python3 "$CADRE_ENGINE_ROOT/lib/go_pencere.py" --ozet 2>/dev/null)"
  [ -n "$pj" ] && printf '  Go pencere: %s\n' "$pj"
  # Item 9: show the contract (budget split) of the running run
  local sz; sz="$(python3 "$CADRE_ENGINE_ROOT/lib/sozlesme.py" ozet 2>/dev/null)"
  [ -n "$sz" ] && printf '  %s\n' "$sz"
  if [ -n "$p" ]; then local pk; pk="$(python3 "$CADRE_ENGINE_ROOT/lib/proje.py" ozet "$p" 2>/dev/null)"   # G-P6: project mode progress
    [ -n "$pk" ] && printf '  proje kipi: %s\n' "$pk"; fi
  kurtarma_uyari | sed "s/^/  ${Y}kurtarma${R} : /"
  if [ -n "$p" ]; then
    local L; L="$(son_log "$p")"
    # if the old engine log line is not found, no empty 'last job' line is printed; the structured state is on the 'run' line
    local SL; [ -n "$L" ] && SL="$(grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8} (BITTI|TAKILDI|devam ediliyor|KURTARICI|kosu basliyor|kosu kendi bitti|IPTAL|BEKCI|cadre-guard)' "$L" | tail -1 | cut -c12-100)"
    [ -n "${SL:-}" ] && printf '  son iş   : %s\n' "$SL"
    local K; K="$(son_defter "$p")"; [ -n "$K" ] && printf '  son kayıt: %s\n' "$(basename "$K")"
    # P4/P5: structured state (run.json): last run state/stage/reason, pending queue, spending — not from the log line
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" ozet "$p" 2>/dev/null | sed 's/^/  koşu     : /'
  fi
  printf '%s───────────────────────────────────────────────%s\n' "$D" "$R"
}

gorev(){
  [ -n "$project" ] || qe_die "bu dizin kayıtlı bir proje değil (projects/ altında repo-path yok)"
  if calisan_var "$project"; then
    printf '%s⚑ %s için zaten bir iş çalışıyor; yeni görev SIRAYA alınır, o bitince kendiliğinden başlar (aynı repoda tek yazar).%s\n' "$Y" "$project" "$R"
  fi
  local task="" line bos=0
  if [ -n "${1:-}" ]; then task="$1"$'\n'; else
  printf '\n%s[%s]%s Görevi yaz/yapıştır. Bitirmek için boş satırda %siki kez Enter%s ya da tek başına %sgo%s yaz:\n\n' "$B" "$project" "$R" "$B" "$R" "$B" "$R"
  # Ctrl-D was confusing on the Mac (Cmd-D splits the terminal). End: a "go" line, two empty lines or EOF.
  [ -t 1 ] && printf '\033[r\033[?7h'
  # `|| [ -n "$line" ]`: also take the LAST line when it ends without a newline
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in go|GO|bitti|BITTI|.) break ;; esac
    if [ -z "$line" ]; then bos=$((bos+1)); [ "$bos" -ge 2 ] && [ -n "$task" ] && break; else bos=0; fi
    task="${task}${line}"$'\n'
  done
  fi
  printf '%s' "$task" | grep -q '[^[:space:]]' || { echo "boş görev — çıkılıyor" >&2; return 1; }
  printf '\n%s%d satır görev alındı.%s\n' "$D" "$(printf '%s\n' "$task" | wc -l | tr -d ' ')" "$R"
  local run_id pf
  run_id="$(printf '%s' "$task" | python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" accept "$project" -)" || return 1
  pf="$CADRE_HOME/runs/$run_id/prompt.md"
  if ! env -u CADRE_RUN_ID python3 "$CADRE_ENGINE_ROOT/lib/izin.py" model-consent "$run_id"; then
    printf 'Görev kaydedildi: %s; kesin model çağrısı izni bekliyor.\n' "$run_id"
    return 0
  fi
  export CADRE_RUN_ID="$run_id"
  bekciler_ayakta
  # P4: if the same project has unfinished work the record is `queued` — a second coordinator is NOT opened; the coordinator/guard takes over when the one ahead finishes.
  local st; st="$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" show "$run_id" | python3 -c 'import json,sys;print(json.load(sys.stdin)["state"])' 2>/dev/null)"
  if [ "$st" = "queued" ]; then
    printf '\n%s⏳ Görev SIRAYA alındı: %s.%s Aynı projede önce biten iş var; o bitince kendiliğinden başlar. Terminali kapatabilirsin.\n' "$Y" "$run_id" "$R"
  else
    nohup bash "$CADRE_ENGINE_ROOT/bin/orkestra.sh" "$project" "$pf" > "$CADRE_HOME/runs/$run_id/start.log" 2>&1 </dev/null &
    disown 2>/dev/null || true
    sleep 1
    printf '\n%s✓ Görev kaydedildi: %s.%s Terminali kapatabilirsin.\n' "$G" "$run_id" "$R"
  fi
  printf '  %sizle: cadre  ·  iptal: cadre iptal  ·  defter (iş bitince): cadre defter%s\n\n' "$D" "$R"
  [ "${CADRE_IZLE:-1}" = "1" ] && [ -t 1 ] && izle "$project"
  return 0
}

izle(){
  local p="${1:-$project}"; [ -n "$p" ] || qe_die "proje yok"
  local rid; rid="${CADRE_RUN_ID:-$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" latest-id "$p")}" || return 1
  if [ -n "$rid" ]; then
    # decision 9: the watcher closes itself when the job ends (izle.py "BITTI" -> 1.5 s) -> delivery offer on the same screen
    python3 "$CADRE_ENGINE_ROOT/lib/izle.py" "$p" --run "$rid" ${CADRE_IZLE_ARGS:-}
    teslim_teklif "$p" "$rid"
    return 0
  fi
  if ! calisan_var "$p" && [ -z "$(son_log "$p")" ]; then echo "izlenecek iş yok" >&2; return 1; fi
  exec python3 "$CADRE_ENGINE_ROOT/lib/izle.py" "$p" ${CADRE_IZLE_ARGS:-}
}

iptal(){
  local p="${1:-$project}"; [ -n "$p" ] || qe_die "proje yok"
  bash "$CADRE_ENGINE_ROOT/bin/cadre-iptal" "$p"
}

# --- DELIVERY: the system never pushes/merges on its own; the user picks from the menu, says "yes" once, and the system does the work.
teslim_adaylari(){ # $1=project -> lines "run_id|worktree|label": completed runs not MERGED yet (pushed ones are labelled)
  python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" list "$1" 2>/dev/null | python3 -c '
import json,os,sys
sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib")
from sonuc import approved
from izin import delivery_candidate
for x in json.load(sys.stdin):
    # B110-3: "completed" is the evidence AT acceptance time; the tree may have changed AT delivery time (manual edit,
    # `cadre devam`, an unfinished repair). approved() re-binds the current revision to the QA/judge evidence:
    # delivered product = reviewed product.
    if delivery_candidate(x):
        et = ("push edildi " + x["teslim_push"]) if x.get("teslim_push") else ("bitti " + str(x.get("finished_at") or x.get("updated_at") or x.get("created") or ""))
        print(x["run_id"] + "|" + x["worktree"] + "|" + et)'
}
sec_kosu(){ # $1=project [$2=run_id] -> "run_id|worktree"; that one if a single candidate, a numbered choice if several (CADRE_SECIM for tests; empty = the latest)
  local p="$1" istenen="${2:-}" satir n i; local -a a=()
  while IFS= read -r satir; do [ -n "$satir" ] && a+=("$satir"); done < <(teslim_adaylari "$p")
  [ "${#a[@]}" -gt 0 ] || return 1
  if [ -n "$istenen" ]; then
    for satir in "${a[@]}"; do case "$satir" in "$istenen|"*) printf '%s' "$satir" | cut -d'|' -f1-2; return 0;; esac; done
    return 1
  fi
  if [ "${#a[@]}" -eq 1 ]; then printf '%s' "${a[0]}" | cut -d'|' -f1-2; return 0; fi
  printf '%sBitmiş, teslim edilmemiş işler:%s\n' "$B" "$R" >&2
  i=0; for satir in "${a[@]}"; do i=$((i+1)); printf '  %d) %s  %s\n' "$i" "$(printf '%s' "$satir" | cut -d'|' -f1 | cut -c1-15)" "$(printf '%s' "$satir" | cut -d'|' -f3-)" >&2; done
  n="${CADRE_SECIM:-}"
  if [ -z "$n" ]; then printf '%sHangisi? (numara, boş = en son): %s' "$Y" "$R" >&2; IFS= read -r n || n=""; fi
  [ -n "$n" ] || n="${#a[@]}"
  case "$n" in *[!0-9]*) qe_err "geçersiz seçim: $n"; return 1;; esac
  [ "$n" -ge 1 ] && [ "$n" -le "${#a[@]}" ] || { qe_err "geçersiz seçim: $n"; return 1; }
  printf '%s' "${a[$((n-1))]}" | cut -d'|' -f1-2
}
teslim_teklif(){ # $1=project $2=run_id — when the watcher closes: if the job is completed and not merged, a one-key delivery offer (decision 9)
  local p="$1" rid="$2" tus
  [ -n "$rid" ] || return 0
  sec_kosu "$p" "$rid" >/dev/null 2>&1 || return 0
  printf '\n%s✔ İş tamamlandı (QA + hakem geçti): %s%s\n' "$G" "$(printf '%s' "$rid" | cut -c1-15)" "$R"
  printf '%s[p]%s push et   %s[m]%s merge et   %s[q]%s çık : ' "$B" "$R" "$B" "$R" "$B" "$R"
  tus="${CADRE_TESLIM_TUS:-}"
  if [ -z "$tus" ]; then
    if [ -t 0 ]; then IFS= read -r -n1 tus || tus=q; echo; else echo; return 0; fi
  else echo "$tus"; fi
  case "$tus" in p|P) push_urun "$p" "$rid" ;; m|M) merge_urun "$p" "$rid" ;; *) printf '%steslim yapılmadı — menüden 5/6 ile sonra yapabilirsin.%s\n' "$D" "$R" ;; esac
}
onay(){ # $1=question $2=env-name -> 0 yes / 1 no. Tests answer with CADRE_ONAY*.
  local cevap="${!2:-}"
  if [ -z "$cevap" ]; then printf '%s%s (evet/hayır): %s' "$Y" "$1" "$R" >&2; IFS= read -r cevap || cevap=""; fi
  case "$cevap" in evet|EVET|e|E|yes) return 0;; *) printf '%svazgeçildi.%s\n' "$D" "$R"; return 1;; esac
}
iki_onay(){
  onay "$1" "$3" || return 1
  printf '%sÇalışacak komut:%s %s\n' "$B" "$R" "$2"
  onay "Bu komut için ikinci onay" "${3}_2"
}
teslim_kaydet(){ printf '{"teslim_%s": "%s"}' "$2" "$(date '+%F %T')" | python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" update "$1" >/dev/null 2>&1 || true; }
onayli_sha(){
  python3 - "$1" <<'PY'
import os, sys
sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib")
from kosu import RunStore
from sonuc import approved
k = RunStore().read(sys.argv[1])
if (k.get('final_result') or {}).get('delivery', {}).get('git_permission') == 'required':
    raise SystemExit(1)
if k.get("state") != "completed" or not k.get("worktree") or "teslim_merge" in k or not approved(k):
    raise SystemExit(1)
print(k["final_result"]["qa"]["revision"]["head"])
PY
}
hala_onayli(){
  [ "$(onayli_sha "$1" 2>/dev/null)" = "$2" ] && return 0
  qe_err "onaydan sonra ürün değişti; incelenen commit $2 değil, teslim yapılmadı"
  return 1
}
ana_repo_hazir(){
  [ "$(git -C "$1" branch --show-current 2>/dev/null)" = "$2" ] || { qe_err "ana repo '$(git -C "$1" branch --show-current 2>/dev/null)' dalında; merge için '$2' dalında olmalı (git -C \"$1\" checkout $2)"; return 1; }
  [ -z "$(git -C "$1" status --porcelain 2>/dev/null)" ] || { qe_err "ana repoda commit'lenmemiş değişiklik var; merge yapılmaz"; return 1; }
}
push_urun(){
  local p="${1:-$project}" bilgi rid
  bilgi="$(sec_kosu "$p" "${2:-}")" || { qe_err "teslim edilecek TAMAMLANMIŞ koşu yok"; return 1; }
  rid="${bilgi%%|*}"
  python3 "$CADRE_ENGINE_ROOT/lib/izin.py" deliver "$rid" push || return 1
}
merge_urun(){
  local p="${1:-$project}" bilgi rid
  bilgi="$(sec_kosu "$p" "${2:-}")" || { qe_err "teslim edilecek TAMAMLANMIŞ koşu yok"; return 1; }
  rid="${bilgi%%|*}"
  python3 "$CADRE_ENGINE_ROOT/lib/izin.py" deliver "$rid" merge || return 1
}

defter(){
  local p="${1:-$project}"; local K; K="$(son_defter "$p")"
  [ -n "$K" ] || { echo "defter yok"; return 1; }
  printf '%s\n' "$K"; printf '%s  transcript.md · meta.json · motor.log · izleyiciler/ · degisiklik.patch%s\n' "$D" "$R"
  command -v code >/dev/null && printf '%s  aç: code "%s/transcript.md"%s\n' "$D" "$K" "$R"
}

menu(){
  baslik; durum
  # user request: ONE command — typing `cadre` shows every subcommand here, picked with the arrow keys.
  local secenek; local -a ops=(
    "$(simge gorev) Yeni görev ver          — görevi yapıştır, git; canlı akış açılır"
    "$(simge izle) Ekranı geri aç          — çalışan işin canlı akışına dön (q ile kapat, iş sürer)"
    "$(simge iptal) İşi iptal et            — çalışan işi güvenle durdur (kod worktree'de kalır)"
    "$(simge ozet) Gece özeti              — koşular, harcama, üç pencere, dikkat edilecekler"
    "$(simge izle) İş kaydı klasörü        — bitmiş işin tam kaydı (analiz için Claude'a ver)"
    "$(simge push) Ürünü push et           — bitmiş işi seç, cadre dalını origin'e gönder (onay ister)"
    "$(simge merge) Kanonik dala merge et   — bitmiş işi seç, ana dala birleştir (onay ister)"
    "$(simge kurtarma) Kurtarma                — askıda kalmış rezervasyon / fiyat arızası: listele, onayla kapat"
    "$(simge bakiye) Bakiye gir/gör          — abonelik ve krediler; bakiye girişi"
    "Çık")
  while :; do
    secenek="$(qe_select "${B}Ne yapalım?${R}" "${ops[@]}")"
    case "$secenek" in
      1) gorev; return $? ;;
      2) izle "$project"; return $? ;;
      3) iptal "$project" ;;
      4) ozet_ekrani ;;
      5) defter "$project" ;;
      6) push_urun "$project" ;;
      7) merge_urun "$project" ;;
      8) kurtarma_menu ;;
      9) bakiye_menu ;;
      0|10|"") return 0 ;;
    esac
  done
}

ozet_ekrani(){
  local tus
  python3 "$CADRE_ENGINE_ROOT/lib/sabah_ozeti.py"
  while [ -t 0 ]; do
    printf '\nd ayrıntı · q geri: '
    IFS= read -r -s -n1 tus || return 0
    printf '\n'
    case "$tus" in
      d|D) python3 "$CADRE_ENGINE_ROOT/lib/sabah_ozeti.py" --ayrinti | ${PAGER:-less -R} ;;
      q|Q|"") return 0 ;;
    esac
  done
}

kurtarma_menu(){
  local s
  s="$(qe_select "${B}Kurtarma${R}" \
      "Askıda rezervasyonları listele" \
      "Askıda rezervasyonları kapat   — rezerve tutar harcanmış sayılır" \
      "Askıda rezervasyonları SIFIRLA — çağrılar hiç yapılmadıysa (para geri döner)" \
      "Fiyat arızasını listele" \
      "Fiyat arızasını temizle" \
      "Geri")"
  case "$s" in
    1) python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" --bayat-listele ;;
    2) onay "Askıda kalan rezervasyonlar kapatılsın mı? (rezerve tutar harcanmış sayılır)" CADRE_ONAY \
         && python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" --bayat-kapat --evet ;;
    3) onay "Rezervasyonlar SIFIRLANSIN mı? Bunu yalnız çağrıların hiç yapılmadığını biliyorsan seç." CADRE_ONAY \
         && python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" --bayat-kapat --sifir --evet ;;
    4) python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" --ariza-listele ;;
    5) onay "Fiyat arızası temizlensin mi? Sebebi giderilmediyse aynı anda geri gelir." CADRE_ONAY \
         && python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" --ariza-temizle --evet ;;
  esac
}

case "${1:-}" in
  "")      if [ -t 0 ] && [ -t 1 ]; then
             py="${CADRE_UI_PYTHON:-$HOME/.local/share/cadre-ui-venv/bin/python}"
             if [ -z "${CADRE_ESKI_MENU:-}" ] && [ -x "$py" ]; then bekciler_ayakta; CADRE_PROJE="$project" "$py" "$CADRE_ENGINE_ROOT/lib/arayuz.py"; else menu; fi
           else gorev; fi ;;
  gorev)   shift; gorev "$*" ;;
  istek)   shift; exec python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" request "$@" ;;
  project-step-verify|request-proposal|request-approve|request-plan|plan-show|plan-backlog|plan-answer|plan-change|plan-stop|plan-withdraw|plan-review|plan-resume|test-maintenance)
    action="$1"; shift; exec python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" "$action" "$@" ;;
  iz)      shift; exec python3 "$CADRE_ENGINE_ROOT/lib/arayuz_veri.py" "$@" ;;
  init)    shift; if [ $# -eq 0 ]; then set -- "$PWD"; fi; python3 "$CADRE_ENGINE_ROOT/lib/proje_kayit.py" "$@" ;;
  izin-cozumle) shift; env -u CADRE_RUN_ID python3 "$CADRE_ENGINE_ROOT/lib/izin.py" resolve "$@" ;;
  izle)    izle "${2:-$project}" ;;
  durum)   if [ "${2:-}" = "--ayrinti" ]; then durum_ayrinti "${3:-$project}"; else durum "${2:-$project}"; fi ;;
  iptal)   iptal "${2:-$project}" ;;
  defter)  defter "${2:-$project}" ;;
  cleanup-backup) python3 "$CADRE_ENGINE_ROOT/lib/izin.py" "$1" "${2:-}" ;;
  cleanup|restore) python3 "$CADRE_ENGINE_ROOT/lib/izin.py" "$1" "${2:-}" ;;
  push)    push_urun "${2:-$project}" "${3:-}" ;;
  merge)   merge_urun "${2:-$project}" "${3:-}" ;;
  kosular) python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" list ${project:+"$project"} ;;
  kurtar)  shift; python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" "${@:---bayat-listele}" ;;
  verim)   shift; exec python3 "$CADRE_ENGINE_ROOT/lib/verim.py" "$@" ;;
  kurulum-kontrol) shift; exec python3 "$CADRE_ENGINE_ROOT/lib/kurulum_kontrol.py" "$@" ;;
  ozet)   shift; python3 "$CADRE_ENGINE_ROOT/lib/sabah_ozeti.py" "$@" ;;
  tasarim) py="${CADRE_UI_PYTHON:-$HOME/.local/share/cadre-ui-venv/bin/python}"; [ -x "$py" ] || qe_die "Textual ortami yok: python3 -m venv ~/.local/share/cadre-ui-venv && ~/.local/share/cadre-ui-venv/bin/pip install textual"; "$py" "$CADRE_ENGINE_ROOT/lib/arayuz.py" --ornek ;;
  zen-bakiye) zen_bakiye ;;
  bakiye)     bakiye_menu ;;
  butce)      python3 "$CADRE_ENGINE_ROOT/lib/opencode_butce.py" ;;
  devam)
    [ -n "${2:-}" ] || qe_die "kullanim: cadre devam RUN_ID"
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" start "$2" || exit 1
    # B110: `--devam` is the operator's EXPLICIT request; it puts a failed run back in. Automatic paths cannot do this.
    printf 'Devam istegi kaydedildi: %s (sahibi varsa ikinci yurutucu baslamaz)\n' "$2"
    ;;
  harcama-onay)   # spending alarm approval; the stopped run restarts (no second executor if it has an owner)
    [ -n "${2:-}" ] || qe_die "kullanim: cadre harcama-onay RUN_ID"
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" harcama-onay "$2" || exit 1
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" start "$2" || exit 1
    ;;
  -h|--help|yardim) cat <<'YARDIM'
cadre — tek komut. Argumansiz calistirinca menu acilir.

  cadre                 menu (gorev, izle, iptal, ozet, teslim, kurtarma, bakiye)
  cadre gorev [metin]   yeni is; metin verilmezse editor acilir
  cadre istek          stdin JSON: panelle ayni tipli kayit; plan kapisi bekler, yurutucu acmaz
  cadre iz ID          salt-okur maskeli olay izi; --format json|markdown, --replay stdin JSON
  cadre init [--olustur] [--tur python] [klasor]   bu klasoru (ya da verileni) Cadre projesi olarak kaydeder; --olustur bos klasoru acar, ilk commit izni ister; --tur ilk commit'e deterministik iskelet koyar ve make install/test/run kaydini alir
  cadre izin-cozumle ID TOKEN etkisiz|tamamlandi   yarim kalan (in-flight/uncertain) izinli eylemi operator karariyla kapatir
  cadre izle            calisan isin canli izleyicisi
  cadre durum [--ayrinti] son kosunun ozeti
  cadre iptal [ID]      calisan isi durdur
  cadre devam ID        dusmus/yarim kosuyu operator olarak yeniden baslat
  cadre harcama-onay ID harcama alarmiyla duran kosuyu onayla ve surdur
  cadre push|merge      biten isi teslim et (her ikisi de onay ister)
  cadre defter|ozet     kosu defteri · sabah ozeti (--ayrinti / --yaz)
  cadre bakiye          zen bakiyesini gir ya da gor
  cadre butce           opencode: Go aboneligi + kredi/zen bakiyesi tek ekranda
  cadre kurtar [...]    askida rezervasyon/hayalet kurtarma araclari; --makbuz-listele [ID] / --makbuz-kabul RES --evet fazla makbuz kabulu
  cadre verim ID        asama/havuz maliyet ve sure kirilimi (kesin/tahmini/askida; bilinmeyen sifir yazilmaz)
  cadre kurulum-kontrol salt-okur onkosul/surum/yol kontrolu; kurmaz, indirmez (--json)
  cadre tasarim            ayni arayuz sahte veriyle (cadre: gercek veriyle; CADRE_ESKI_MENU=1 eski menu)
YARDIM
    python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from sinirlar import motor_sureleri
try:
    warning, limit = motor_sureleri()
    print(f'Motor suresi: uyari {warning} sn (0=kapali), kesin son {limit} sn; devamda saat sifirlanmaz.')
    print('CADRE_WARN_SEC / CADRE_ABORT_SEC; uyari isi kesmez, kesin son ilerleme olsa da uygulanir.')
except ValueError:
    print('Motor suresi ayari gecersiz: uyari >=0, kesin son >0 tam saniye olmali.', file=sys.stderr)
    raise SystemExit(2)
PY
    ;;
  *) echo "bilinmeyen: $1 (cadre | gorev | init | izle | durum | iptal | defter | push | merge | kurtar | zen-bakiye | harcama-onay | ozet | tasarim)" >&2; exit 2 ;;
esac
