#!/usr/bin/env bash
# cadre-dispatch.sh — DISPATCHER V1 coordinator
#   cadre-dispatch.sh <project> <prompt-file>
# Exit: 0 = job DONE via dispatch, 1 = job PARTIAL/cancelled via dispatch, 3 = DISPATCH SKIPPED (fall back)
# Default OFF: returns 3 immediately unless CADRE_DISPATCH=1. Single writer: only this process writes the state JSON files.
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
REAL="${CADRE_REAL_OPENCODE:-/opt/homebrew/bin/opencode}"
[ -x "$REAL" ] || REAL="$(PATH=/opt/homebrew/bin:/usr/local/bin command -v opencode)"
proje="${1:?proje}"; pf="${2:?prompt dosyasi}"
[ -n "${CADRE_RUN_ID:-}" ] || exec bash "$CADRE_ENGINE_ROOT/bin/orkestra.sh" "$proje" "$pf"
if [ "${CADRE_TEK_SERIT:-0}" = 1 ]; then
  exec python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" serit "$proje" "$pf"
fi
[ "${CADRE_DISPATCH:-1}" = "1" ] || [ "${CADRE_DUZELTME_YALNIZ:-0}" = 1 ] || exit 3   # B274: the correction loop is part of acceptance
[ -s "$pf" ] || exit 3
Z="$CADRE_RUN_DIR"; CAL="$Z/tasks"; mkdir -p "$CAL"
DLOG="$Z/dispatch.log"
dlog(){ printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$DLOG" >&2; }
task_dir(){ python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-path "$CADRE_RUN_ID" "$1"; }
task_state(){ python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-state "$CADRE_RUN_ID" "$1"; }
# Shared functions must be defined even when the run continues with a recorded plan (the plan branch is skipped).
bulut_bekle(){ # design 3.3: $1=role $2=class $3=score; on hard+ work without a strong cloud, waits if the share/window renews within <=2 h (once per dispatcher)
  [ -z "${BULUT_BEKLENDI:-}" ] || return 1
  local sn s; sn="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" bekleme "$1" "$2" "${3:-0}" 2>>"$DLOG")"
  [ "${sn:-0}" -gt 0 ] 2>/dev/null || return 1
  BULUT_BEKLENDI=1
  dlog "BUTCE BEKLEME ($1): zor+ is, guclu butceli bulut yok; pay/pencere yenilenmesine $(( (sn + 59) / 60 )) dk — bekleniyor"
  while [ "$sn" -gt 0 ]; do
    [ -f "$CADRE_RUN_DIR/cancel.flag" ] && return 1
    s=$(( sn > 300 ? 300 : sn )); sleep "$s"; sn=$(( sn - s ))
    [ "$sn" -gt 0 ] && dlog "BUTCE BEKLEME: kalan $(( (sn + 59) / 60 )) dk"
  done
  dlog "BUTCE BEKLEME bitti ($1)"
}
planlayici_cagir(){ # $1=model $2=prompt $3=task id $4=sandbox $5=difficulty [$6=deadline role: plan|bolme, sinirlar.json] -> raw output
  CADRE_GOREV_ID="$3" RB="$REAL" DM="$1" PP="$2" PLAN_DIFFICULTY="$5" SURE_ROL="${6:-plan}" python3 -c "
import os,sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore, capture_source_plan
from sinirlar import son_sure
try:
    print(capture_source_plan(RunStore(), os.environ['CADRE_RUN_ID'], os.environ['RB'], os.environ['DM'],
          os.environ['PP'], os.environ['CADRE_GOREV_ID'], son_sure(os.environ['SURE_ROL']), int(os.environ['PLAN_DIFFICULTY'])))
except (ValueError,PermissionError) as e:
    print('HATA',e,file=sys.stderr); sys.exit(78)
except Exception as e: print('HATA',e,file=sys.stderr)
" 2>>"$DLOG"
}

ek_serit_kos(){ # $1=lane id $2=model(empty=the router chooses) $3=prompt file -> uretti|yarim|iptal; no helper, the same scope check (G-C15)
  local sid="$1" once="$Z/.serit-once-$1.json"
  python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" serit-durumu "$WT" > "$once" 2>>"$DLOG" || : > "$once"
  env CADRE_WORKTREE="$WT" CADRE_GOREV_ID="$sid" CADRE_RUN_MODEL="$2" CADRE_ALT_AJANSIZ=1 \
      bash "$CADRE_ENGINE_ROOT/bin/is-motoru.sh" "$proje" "$3" >>"$DLOG" 2>&1
  [ -s "$once" ] && python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" kapsam "$WT" "$Z/plan.json" "$sid" "$once" "$Z/kapsam-disi" 2>>"$DLOG" | while IFS= read -r s; do dlog "$s"; done
  local d; d="$(task_state "$sid")"; echo "${d:-yarim}"
}
kabul(){ env CADRE_WORKTREE="$WT" CADRE_GOREV_ID=kalite CADRE_REAL_OPENCODE="$REAL" \
    python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" finish "$proje" "$WT" "$KM" >>"$DLOG" 2>&1; }
duzeltme_kos(){ # G-C16: $1=round $2=model(empty=the router chooses) -> uretti|yarim|iptal; findings in the prompt, no helper, the same scope check
  local gid="duzeltme-$1" GP
  GP="$(task_dir "$gid")/prompt.md" || { echo yarim; return; }
  python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" duzeltme-istemi "$CADRE_RUN_ID" "$1" "$DUZ_UST" > "$GP" 2>>"$DLOG" || { echo yarim; return; }
  ek_serit_kos "$gid" "$2" "$GP"
}
duzeltme_dongusu(){ # G-C16: acceptance + limited correction rounds; $1=atla -> the first acceptance already failed (B274: the plain engine path)
  local ilk="${1:-}" karar dtur dimza dmodel drol dsonuc n_commit
  DUZ_UST="${SEV_TUR:-2}"   # G-C16 round allowance comes from the level (G-E9); 2 when there is no level
  while :; do
    if [ "$ilk" = atla ]; then ilk=""; else kabul && break; fi
    [ -f "$Z/cancel.flag" ] && { dlog "IS IPTAL (bayrak)"; return 130; }
    DUZ_YENIDEN=0; case "${SEV_AD:-}" in zor|cok-zor|ultra) DUZ_YENIDEN=1;; esac   # hard+: if it does not converge, one clean restart
    karar="$(python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" duzeltme-karari "$CADRE_RUN_ID" "$DUZ_UST" "$DUZ_YENIDEN" 2>>"$DLOG")"
    if [ "${karar%% *}" != "DEVAM" ]; then
      dlog "DUZELTME YOK: ${karar#* }"; dlog "DISPATCH IS YARIM (QA/kabul/kalite gecmedi). worktree=$WT"
      printf '%s\n' "$WT"; return 1
    fi
    read -r _ dtur dimza <<<"$karar"
    drol=impl
    if [ -n "$RAPOR_KIPI" ]; then drol=plan   # G-R6: report correction also writes interpretation/results -> strong model
    elif [ "$dtur" = yeniden ]; then drol=kurtarici; else drol=impl; fi   # every round in the cloud (P3); clean restart with the fallback chain
    if [ -n "$drol" ]; then
      CADRE_GOREV_ID="duzeltme-$dtur" bulut_bekle "$drol" orta "${SEV_PUAN:-0}" || true   # B82 hand-off: the task id opens the acceptance pocket
      dmodel="$(CADRE_GOREV_ID="duzeltme-$dtur" python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk "$drol" orta 30000 2>/dev/null)"
    fi
    if [ -z "$dmodel" ]; then
      dlog "DUZELTME YOK: bulut kapasitesi bekleniyor"; dlog "DISPATCH IS YARIM (QA/kabul/kalite gecmedi). worktree=$WT"
      printf '%s\n' "$WT"; return 1
    fi
    dlog "DUZELTME TURU $dtur/$DUZ_UST ($dimza) model=$dmodel"
    dsonuc="$(duzeltme_kos "$dtur" "$dmodel")"
    n_commit="$(qe_commit_wip "$WT" "$proje" "duzeltme turu $dtur" 2>>"$DLOG")"; dlog "duzeltme $dtur: $dsonuc, commit ${n_commit:-0} dosya"
  done
  dlog "DISPATCH IS TAMAM (QA/kabul/kalite gecti). worktree=$WT"
  printf '%s\n' "$WT"; return 0
}
# B274: if the plain engine (no plan / pre-filter skip / no budget) fell with a fixable FAIL, the coordinator
# calls the dispatcher only for this loop; no plan and no lanes, same G-C16 rules.
if [ "${CADRE_DUZELTME_YALNIZ:-0}" = 1 ]; then
  WT="${CADRE_WORKTREE:?worktree}"; KM=""; RAPOR_KIPI=""
  dlog "SADE MOTOR KABULU DUSTU -> G-C16 duzeltme dongusu (B274) worktree=$WT"
  duzeltme_dongusu "$([ "${CADRE_DUZELTME_ATLA:-0}" = 1 ] && echo atla)"; exit $?
fi
if [ -f "$Z/plan.json" ]; then
  plan_json="$(cat "$Z/plan.json")" || exit 1
  dlog "kayitli plan ile devam: $CADRE_RUN_ID"
else

BUTCE_JSON="$(python3 "$CADRE_ENGINE_ROOT/lib/butce.py" 2>/dev/null)"
[ -n "$BUTCE_JSON" ] || { dlog "butce hesaplanamadi -> atla"; exit 3; }

# --- PRE-FILTER (§2): size + budget rule; a free LLM (groq) may raise the class, never lower it (onelek.py) ---
repo="$(qe_repo_path "$proje")" || exit 3
read -r ONELEK ONELEK_PREM <<EOF2
$(printf '%s' "$BUTCE_JSON" | python3 "$CADRE_ENGINE_ROOT/lib/onelek.py" "$pf" "$repo")
EOF2
[ "$ONELEK" = "GECER" ] || { dlog "on-elek: ATLA (boyut/butce) -> kural-tabanli"; exit 3; }
if [ "$ONELEK_PREM" = "PREMIUM" ]; then   # B199: a hard job is planned within its own cap (the Claude plan and role shares see this cap)
  read -r ESKI_TAVAN YENI_TAVAN <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" zor-tavan "$CADRE_RUN_ID" "$proje" 2>>"$DLOG")"
  [ "${ESKI_TAVAN:-x}" = "${YENI_TAVAN:-x}" ] || dlog "zor is: kosu tavani \$${ESKI_TAVAN} -> \$${YENI_TAVAN}"
fi

# --- PLAN GENERATION (§2): pick the dispatcher model, run it in an empty sandbox ---
GO_ORAN=$(printf '%s' "$BUTCE_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['go']['oran'])")
CLAUDE_PAY=$(printf '%s' "$BUTCE_JSON" | python3 -c "import json,sys;c=json.load(sys.stdin).get('claude',{});print(f\"%{c.get('pay',0)} (~\${c.get('kullanilabilir_usd',0)})\")")   # C31: remaining share of the weekly reserve; no daily dollar cap
PENCERE=$(printf '%s' "$BUTCE_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['go']['pencere_acik'])")
# PLAN MODEL: ROUTER (stage 10) — plan chain (if there is a Claude share -> Sol -> Go -> Zen)
SINIF="orta"; [ "$ONELEK_PREM" = "PREMIUM" ] && SINIF="zor"
KAPSAM_KURAL="$(python3 "$CADRE_ENGINE_ROOT/lib/seviye.py" kural "$pf" 2>>"$DLOG")"   # G-E9: plan limit from the max(rule, planner) level
DMODEL="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk plan "$SINIF" 30000 2>/dev/null)"
if bulut_bekle plan "$SINIF" "$KAPSAM_KURAL"; then DMODEL="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk plan "$SINIF" 30000 2>/dev/null)"; fi
case "$DMODEL" in claude/*) DMODEL="${DMODEL%%#*}";; esac
[ -n "$DMODEL" ] || { dlog "plan icin butceli bulut yok -> bulut kapasitesi bekleyen fallback"; exit 3; }   # S5: no planning with Go when there is no budget
dlog "plan modeli (yonlendirici): $DMODEL"

HARITA_TUMU=$(cd "$repo" && find . -maxdepth 2 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name 'Makefile' -o -name '*.toml' \) 2>/dev/null | grep -v '/\.git/')
HARITA_TOPLAM=$(printf '%s\n' "$HARITA_TUMU" | grep -c . || true)
HARITA=$(printf '%s\n' "$HARITA_TUMU" | head -60)
HARITA_NOT="ilk 60 / toplam $HARITA_TOPLAM dosya"
if [ "${HARITA_TOPLAM:-0}" -gt 60 ]; then HARITA_NOT="$HARITA_NOT; KIRPILDI, kalan dosyalari salt-okur araclarla oku"; fi
dlog "repo haritasi: $HARITA_NOT"
SANDBOX="$(task_dir plan)/sandbox" || exit 1
mkdir -p "$SANDBOX" || exit 1
PLAN_PROMPT="SEN BIR GOREV PLANLAYICISIN. Gercek kaynak dosyalarini salt-okur araclarla incele; SADECE tek bir JSON blogu yaz.
Asagidaki isi kapsamina gore 1-8 bagimsiz goreve bol. Kurallar:
- KAPSAM PUANI (zorunlu, G-E9): ISTEGIN tamamina 0-100 puan ver, kendi planina degil: 0-15 <15 dk tek dosya/satir; 16-40 15-60 dk fonksiyon+test; 41-65 1-4 sa cok dosyali ozellik; 66-85 >4 sa modul/refactor; 86-100 gunler: sifirdan proje ya da 8 goreve sigmayan cok maddeli backlog. Kapsami kendiliginden daraltma veya genisletme; rutin karari gerekceli varsayimla kaydet, kritik amac/yetki/tavan belirsizligini karar icin bildir. Istek 8 goreve sigmiyorsa 86+ ver; sistem isi proje kipinde ozelliklere boler. Gorev sayisi puana uysun (basit 1, orta en cok 3, zor en cok 5, cok zor en cok 8); kapsam_gerekce tek cumle.
- 'dosyalar' YALNIZ yol icerir (aciklama/parantez yazma; adi henuz belli olmayan yeni dosya icin klasoru yaz).
- Dosya ortusmelerini acik yaz; siraya ihtiyaci olan gorevleri bagimli alaninda belirt. Koordinator bagimli'si BOS ve dosyalari ayrik bagimsiz bulut gorevlerini ESZAMANLI, digerlerini seri calistirir.
- SECIM KURALI: once model, sonra acik saglayici yolu. Kod yazan roller deepseek-v4-pro › glm-5.3 › qwen3.8-max kullanir (once Go, sonra OpenRouter). Bedava model ve alt ajan yok.
- CLAUDE yalniz plan zincirinde mevcut kota kuraliyla kullanilir (pay: $CLAUDE_PAY).
- adaylar: ds-pro=kod yazan model; auto=yonlendirici zorluga/butceye gore secer. Yerel model YOK; her gorev bulutta kosar.
- paralel_grup: 0..100 (03.10 B340: alan paralellikte ANLAM TASIMAZ — belirleyici 'bagimli bos + dosyalar ayrik'). Ayni dosyaya dokunan gorevleri ayrik dosyalara bol ya da bagimli yap.\n- ZORLUK->MODEL: kod gorevleri ds-pro; zorluk 1-2 ucuz zincir. Kapasite yoksa butce beklenir.\n- ORTAK BELGE (BACKLOG.md, README) yalniz SON gorevde guncellenir; diger gorevler ona dokunmaz. YENI engine dosyasi ekleyen gorev docs/SOZLESME.md kod haritasina o dosyanin satirini KENDISI ekler (sozlesme bekcisi testi ister). Dosyalari ayrisan bagimsiz kod gorevlerini bagimli YAPMA (bagimli bos kalinca paralel kosulur; 03.10 B340).\n- dosyalar alani KLASOR degil DOSYA duzeyinde olsun (engine/lib/butce.py; 'engine' degil): kaba klasor listesi her gorevi cakisiyor gosterip paralelligi oldurur.
- Son gorev HARIC birlestirme/review EKLEME (koordinator ekler).
- Yalniz dogrulama/QA gorevi (ornegin sadece make check/test kosan gorev) EKLEME: koordinatorun QA kapisi ve onarim turu bunu yapar.
- PLAN STANDARDI (her gorevde ZORUNLU): kabul=olculebilir kriter (hangi test/cikti gecerse kabul edilir); kapsam_disi=dokunulmayacaklar; bagimli=once bitmesi gereken gorev id'leri (yoksa bos). Yurutucu ajan SADECE plandaki bilgiyi gorur — belirsiz tanim yazma.
JSON semasi (BASKA HICBIR SEY YAZMA):
{\"is_id\":\"$(basename "$pf" .md)\",\"kapsam_puani\":30,\"kapsam_gerekce\":\"...\",\"gorevler\":[{\"id\":\"g1\",\"tanim\":\"...\",\"dosyalar\":[\"yol\"],\"kabul\":\"olculebilir kabul kriteri (test/cikti)\",\"kapsam_disi\":[\"dokunulmayacak\"],\"bagimli\":[],\"zorluk\":3,\"model\":\"ds-pro\",\"paralel_grup\":1,\"tahmin_dk\":15,\"tahmin_usd\":0.02}],\"birlestirme_sirasi\":[\"g1\"],\"kalite\":{\"model\":\"glm-5.2|ds-pro|oz\",\"kapsam\":\"...\"}}

GECMIS MALIYET (defter: gecmis kosularda gorev basina gercek ortalama — ucretli modele giden gorevde tahmin_usd'yi buna yakin ver, 0 yazma):
$(python3 "$CADRE_ENGINE_ROOT/lib/tahmin_gercek.py" maliyet 2>/dev/null || echo 'henuz olcum yok')

REPO HARITASI ($HARITA_NOT):
$HARITA

IS:
$(cat "$pf")"
RAPOR_EK="$(python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" plan-istemi 2>>"$DLOG")"
[ -z "$RAPOR_EK" ] || PLAN_PROMPT="$PLAN_PROMPT

$RAPOR_EK"
KABUL_QA="$(python3 -c "
import json,os
try: print(((json.load(open(os.environ['CADRE_RUN_DIR']+'/run.json')).get('feature_acceptance') or {}).get('qa_komut') or '').strip())
except Exception: pass" 2>/dev/null)"
[ -z "$KABUL_QA" ] || PLAN_PROMPT="$PLAN_PROMPT

OZELLIK KABUL KOMUTU (QA kapisi YALNIZ bunu kosar): $KABUL_QA
Yeni testleri bu komutun kostugu test dosyalarina yaz ve o dosyalari ilgili gorevin 'dosyalar' listesine ekle; baska yola yazilan test kabulu kanitlamaz."

plan_json=""
RED_ONCE=$(python3 -c "
import json,os
try: print(len(json.load(open(os.environ['CADRE_RUN_DIR']+'/run.json')).get('denials') or []))
except Exception: print(0)" 2>/dev/null || echo 0)
# B171: only attempts where the model was called count (a budget denial is refunded); the round cap stops a chain cycling between denying links.
python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" plan-guvence >>"$DLOG" 2>/dev/null || true   # 4b: plan reserve on Zen/OR (non-blocking)
deneme=0; tur=0
while [ "$deneme" -lt 3 ] && [ "$tur" -lt 10 ]; do
  tur=$((tur + 1)); deneme=$((deneme + 1))
  dlog "plan deneme $deneme (tur $tur), dispatcher=$DMODEL"
  ZORLUK=3; [ "$SINIF" = "zor" ] && ZORLUK=4   # C31: the policy derives effort from difficulty (max only at difficulty 5)
  [ "${DMODEL#claude/}" = "$DMODEL" ] || dlog "claude-plan yolu (istek: $DMODEL, zorluk=$ZORLUK; effort politikadan)"
  RAW=$(planlayici_cagir "$DMODEL" "$PLAN_PROMPT" plan "$SANDBOX" "$ZORLUK") || exit $?
  SEMA_DOSYA="${DLOG%/*}/.plan-sema-hata"
  plan_json=$(printf '%s' "$RAW" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" extract ${KAPSAM_KURAL:+"$KAPSAM_KURAL"} 2>"$SEMA_DOSYA") || plan_json=""
  # B126: on a budget denial the model is not called, output comes back EMPTY and `extract` counts it as a "schema violation";
  # the correction branch went back to the SAME model and retried in vain. Signal: denial text in RAW + growth of `denials[]`.
  RED_SONRA=$(python3 -c "
import json,os,sys
try: print(len(json.load(open(os.environ['CADRE_RUN_DIR']+'/run.json')).get('denials') or []))
except Exception: print(0)" 2>/dev/null || echo 0)
  BUTCE_REDDI=0
  printf '%s' "$RAW" | grep -q 'CADRE_BUDGET_DENIED\|allocation exhausted\|run budget exhausted\|dogrulanmis pay yok' && BUTCE_REDDI=1
  [ "${RED_SONRA:-0}" -gt "${RED_ONCE:-0}" ] && BUTCE_REDDI=1
  RED_ONCE="$RED_SONRA"
  [ "$BUTCE_REDDI" -eq 1 ] || { [ -s "$SEMA_DOSYA" ] && cat "$SEMA_DOSYA" >>"$DLOG"; }   # a denial is not a schema violation; it has its own line below
  SEMA_HATA="$(grep -m1 '^SEMA: ' "$SEMA_DOSYA" 2>/dev/null || true)"
  [ -n "$plan_json" ] && break
  [ "$BUTCE_REDDI" -eq 1 ] || dlog "plan JSON bozuk/sema-ihlali (deneme $deneme); RAW son 300: $(printf '%s' "$RAW" | tail -c 300 | tr '\n' ' ')"
  # B95: three plan models got stuck on the SAME schema rule and their plans were thrown away.
  # A schema violation is NOT a provider failure: instead of switching links, the error goes back to the model and the same model gets
  # one correction chance; it is not written to the circuit breaker either.
  if [ "$BUTCE_REDDI" -eq 0 ] && [ -n "$RAW" ] && [ -n "$SEMA_HATA" ] && [ "${SEMA_DUZELTME:-0}" -eq 0 ]; then
    SEMA_DUZELTME=1
    dlog "plan ${SEMA_HATA} — sema ihlali; model arizasi sayilmadi, duzeltme istegiyle ayni modele tekrar"
    PLAN_PROMPT="$PLAN_PROMPT

ONCEKI DENEME REDDEDILDI: ${SEMA_HATA#SEMA: }
Ayni semayi ve kayitli kapsami koru, YALNIZ bu hatayi duzelt. Rutin karari gerekceli varsayimla kaydet; kritik amac/yetki/tavan belirsizligini karar icin bildir, kapsami kendiliginden genisletme.
Dosya yollari worktree koklu GORELI olmali: mutlak yol yok,
'..' yok, glob (*, ?, []) yok, ':' ve ',' yok. Her gorevin 'dosyalar' listesi bos olmamali."
    continue
  fi
  # B52: a budget/cap denial is NOT a provider FAILURE — not written to the circuit breaker (live run 1: a Claude 403 denial was counted as a failure)
  if [ "$BUTCE_REDDI" -eq 1 ]; then
    dlog "plan BUTCE REDDI ($DMODEL) — sema ihlali degil, ariza da degil; dogrudan sonraki halka"
    deneme=$((deneme - 1))
  elif [ -z "$RAW" ]; then
    dlog "plan denemesi BOS cikti ($DMODEL) — sema ihlali sayilmaz; sonraki halka"
    python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ariza "$DMODEL" "plan bos cikti" >/dev/null 2>&1
  else
    python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ariza "$DMODEL" "plan bozuk/bos" >/dev/null 2>&1
  fi
  SEMA_DUZELTME=0   # one more correction chance for the new link
  NXT="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" sonraki plan "$DMODEL" 2>/dev/null)"
  [ -n "$NXT" ] || { dlog "plan zinciri tukendi -> sade motor"; break; }
  DMODEL="$NXT"
  dlog "plan icin sonraki halka: $DMODEL"
done
[ -n "$plan_json" ] || [ "$tur" -lt 10 ] || dlog "plan tur tavani (10) doldu"
# Retain the planner directory: Claude resume is tied to its original cwd.
[ -n "$plan_json" ] || { dlog "plan uretilemedi -> bulut kural-tabanli fallback"; exit 3; }

printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" save-plan "$CADRE_RUN_ID" "$DMODEL" || exit 1
python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" plan-guvence-birak >>"$DLOG" 2>/dev/null || true
fi
dlog "plan kabul: $(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" ozet)"
python3 "$CADRE_ENGINE_ROOT/lib/butce_kapisi.py" --ilerleme plan-kabul "$(printf '%s' "$plan_json" | shasum -a 256 | cut -c1-64)" 2>>"$DLOG" || true
python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" plan-ready "$CADRE_RUN_ID" >>"$DLOG" 2>&1 || exit 78
RAPOR_KIPI="$(python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" kip 2>/dev/null)"   # G-R1: report declaration in the acceptance record
rm -f "$Z/ek-serit.flag"   # B226: if the previous dispatcher died in the middle of G-R7, a stale hand-off marker must not stop it forever
[ -z "$RAPOR_KIPI" ] || dlog "RAPOR KIPI (G-R1): yalniz rapor + kanit eki degisebilir; rapor QA'si + hakem yoklamasi"
# G-E9: final scope = max(rule floor, planner); the level gives the cap, the correction round and the plan ceiling (before role shares)
read -r SEV_AD SEV_PUAN SEV_TAVAN SEV_TUR SEV_PLAN <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/seviye.py" uygula "$CADRE_RUN_ID" "$proje" 2>>"$DLOG")"
if [ -n "${SEV_AD:-}" ]; then
  dlog "KAPSAM: puan $SEV_PUAN -> seviye $SEV_AD (tavan \$$SEV_TAVAN, duzeltme $SEV_TUR tur, plan en cok $SEV_PLAN gorev)"
  if [ "$SEV_AD" = ultra ] && [ "${CADRE_TYPED_REQUEST:-0}" != 1 ]; then   # G-P1: if the planner also found it large (>=66), the project coordinator instead of plan lanes
    if python3 "$CADRE_ENGINE_ROOT/lib/proje.py" uygun "$CADRE_RUN_ID" 2>>"$DLOG"; then
      dlog "KAPSAM: ultra — PROJE KIPI (G-P1): ozellik listesi + cocuk kosular, ara dal cadre/proje-*"
      python3 "$CADRE_ENGINE_ROOT/lib/proje.py" baslat "$CADRE_RUN_ID" "$proje" >>"$DLOG" 2>&1
      exit $?
    fi
    dlog "KAPSAM: ultra ama planlayici isi buyuk bulmadi (<66) — proje kipi yok, cok zor gibi islenir"
  fi
fi

GIDS=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" validate) || { dlog "plan bagimliliklari gecersiz"; exit 1; }

# TASK CONTRACT: the budget split is written AT plan time and does not change during the run.
# Lanes 75% · judge 15% · helper 10%. Written once; a second call returns the existing one.
printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/sozlesme.py" yaz "$CADRE_RUN_ID" >/dev/null 2>>"$DLOG" \
  && dlog "sozlesme: $(python3 "$CADRE_ENGINE_ROOT/lib/sozlesme.py" ozet "$CADRE_RUN_ID" 2>/dev/null)" \
  || dlog "UYARI sozlesme yazilamadi — varsayilan paylar gecerli"

python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" guvence >>"$DLOG" 2>&1 || exit 75

# --- EXECUTION (§4): one worktree, one writer ---
ts="$(date +%Y%m%d-%H%M%S)"
WT="${CADRE_WORKTREE:-}"
if [ -z "$WT" ]; then
  WT="$(qe_make_worktree "$proje" "dispatch $ts")" || { dlog "worktree acilamadi"; exit 1; }
fi
if ! guard_cikti="$( (qe_verify_ai_worktree "$WT" "$proje") 2>&1 )"; then
  dlog "$guard_cikti"
  python3 -c 'import json,sys;print(json.dumps({"reason": "worktree-guard: " + sys.argv[1].removeprefix("cadre: ")[:300]}))' "$guard_cikti" \
    | python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" update "$CADRE_RUN_ID" >/dev/null 2>&1
  exit 1
fi
python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" worktree "$CADRE_RUN_ID" "$WT" || exit 1
printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" yol-dogrula "$WT" || { dlog "plan yollari dogrulanamadi"; exit 1; }
dlog "worktree: $WT"
if [ "$(printf '%s' "$plan_json" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("kind", ""))')" = verified-backlog ]; then
  KM=""
  kabul
  exit $?
fi
model_map(){ python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" kisa "$1" 2>/dev/null; }   # Stage 10: short name -> model (router)
diger_provider(){ # $1=model $2=lane; stage 10: the next healthy link. B52/B300: a lane left partial by a budget denial is not a failure
  python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" butce-reddi "$CADRE_RUN_ID" "${2:-}" "${1%%/*}" >/dev/null 2>&1 \
    || python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ariza "$1" "gorev yarim" >/dev/null 2>&1
  python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" alternatifler impl "$1" 3 2>/dev/null; }

gorev_pf(){ # $1=id -> create the prompt file
  local gid="$1" GP; GP="$(task_dir "$1")/prompt.md" || return 1
  [ -s "$GP" ] && { echo "$GP"; return; }
  printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" gorev_pf "$gid" | python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" write-prompt "$CADRE_RUN_ID" "$gid" || return 1
  echo "$GP"
}

serit_baslat(){ # $1=id $2=model(empty=the router chooses); puts the pid in SERIT_PID (for wait in the same shell)
  local gid="$1" mdl="$2" GP GIDP="$1"
  printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" yol-dogrula "$WT" || return 1
  GP=$(gorev_pf "$gid") || return 1
  env CADRE_WORKTREE="$WT" CADRE_GOREV_ID="$GIDP" CADRE_RUN_MODEL="$mdl" \
      CADRE_ALT_AJANSIZ=1 \
      CADRE_ABORT_SEC="$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" abort "$gid")" \
      bash "$CADRE_ENGINE_ROOT/bin/is-motoru.sh" "$proje" "$GP" >>"$DLOG" 2>&1 &
  SERIT_PID=$!
}

# The locked run.json is the authoritative task table.

gecmis_yaz(){ # id start_model state duration_min; B294: fiili_model is an old field (= start), hand-offs come from the attempt records
  local mod; mod="$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-models "$CADRE_RUN_ID" "$1" 2>>"$DLOG")"
  printf '{"ts":"%s","proje":"%s","is_id":"%s","gorev_id":"%s","fiili_model":"%s","baslangic_model":"%s",%s,"durum":"%s","sure_dk":%s,"neden":"%s"}\n' \
    "$(date +%FT%T)" "$proje" "$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" is_id)" \
    "$1" "${2:-auto}" "${2:-auto}" "${mod:-\"denenen_modeller\":[]}" "$3" "${4:-0}" "${5:-}" >> "$Z/atama-gecmisi.jsonl"
}

kosu_tek(){ # $1=id $2=model -> wait, print the result (uretti/yarim/iptal)
  local gid="$1" mdl="$2" s0 s1 i worker_rc
  s0=$(date +%s); serit_baslat "$gid" "$mdl" || { echo yarim; return; }
  wait "$SERIT_PID" 2>/dev/null; worker_rc=$?
  s1=$(date +%s)
  local d n; d="$(task_state "$gid")"; d="${d:-yarim}"
  [ "$worker_rc" = 0 ] || { [ "$d" = iptal ] || d=yarim; }
  n=$(awk '{print $2}' "$(task_dir "$gid")/status.txt" 2>/dev/null)
  gecmis_yaz "$gid" "${mdl:-auto}" "$d" "$(( (s1-s0)/60 ))" "$n"
  echo "$d"
}

gorev_kos_zincirli(){ # threshold chain: assigned -> other provider -> failed; $3=rapor: plan chain (G-R6)
  local gid="$1" mdl="$2" kip="${3:-}" d dfiles
  arac_bekliyor(){ python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" show "$CADRE_RUN_ID" | jq -e '.tool_boundary_wait // empty' >/dev/null; }
  case "$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-result "$CADRE_RUN_ID" "$gid")" in
    uretti|tamam) echo uretti; return;;
  esac
  dfiles=$(printf '%s' "$plan_json" | GID="$gid" python3 -c "import json,sys,os;print(','.join([g['dosyalar'] for g in json.load(sys.stdin)['gorevler'] if g['id']==os.environ['GID']][0]))" 2>/dev/null)
  # P2 + B17: cheap syntax check after EVERY attempt — the old code only validated the first attempt,
  # so a fallback provider's broken output counted as 'produced' and went all the way to QA.
  kosu_dogrulu(){ # $1=id $2=model -> uretti|yarim|iptal (a broken product = yarim)
    local r once="$Z/.serit-once-$1.json"
    python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" serit-durumu "$WT" > "$once" 2>>"$DLOG" || : > "$once"
    r=$(kosu_tek "$1" "$2")
    if [ "$r" = "uretti" ] && [ -s "$once" ]; then   # B192: a new file outside the plan is removed from the product, a changed existing file goes to the judge
      python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" kapsam "$WT" "$Z/plan.json" "$1" "$once" "$Z/kapsam-disi" 2>>"$DLOG" | while IFS= read -r s; do dlog "$s"; done
    fi
    if [ "$r" = "uretti" ] && [ -n "$dfiles" ] && ! python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" dogrula "$WT" "$dfiles" 2>>"$DLOG"; then
      dlog "gorev $1: hizli-dogrula BASARISIZ (${2:-auto} ciktisi bozuk) -> escalate"; r=yarim
    fi
    echo "$r"
  }
  d=$(kosu_dogrulu "$gid" "$mdl")
  [ "$d" = "uretti" ] || [ "$d" = "iptal" ] || arac_bekliyor && { echo "$d"; return; }
  if [ "$kip" = rapor ]; then   # G-R6: the report writer moves to the next link of the plan chain; the local 27B model does not write results
    local alt="$mdl" denenen=" $mdl " n=0
    while [ "$n" -lt 4 ]; do
      n=$((n + 1))
      alt="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" sonraki yazar "$alt" 2>/dev/null)" || break
      [ -n "$alt" ] || break
      case "$denenen" in *" $alt "*) break;; esac
      denenen="$denenen$alt "
      dlog "gorev $gid (rapor yazari) yarim -> plan zincirinde sonraki: $alt"
      d=$(kosu_dogrulu "$gid" "$alt"); [ "$d" = "uretti" ] || [ "$d" = "iptal" ] || arac_bekliyor && { echo "$d"; return; }
    done
    if python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" kurtar "$gid" >>"$DLOG" 2>&1; then   # B238: the product is on disk and passes the gate
      dlog "gorev $gid KURTARILDI (B238): yazar zinciri tukendi ama diskteki rapor + kanit eki rapor QA'sindan gecti -> uretti"
      python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task "$CADRE_RUN_ID" "$gid" uretti || exit 1; echo uretti; return
    fi
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" son-deneme-butce "$CADRE_RUN_ID" "$gid" 2>/dev/null && CADRE_GOREV_ID="$gid" python3 -c 'import os,sys; sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib"); from qe_core import butce_bekleme_kaydet; from pathlib import Path; butce_bekleme_kaydet(Path(os.environ["CADRE_RUN_DIR"])/"dispatch.log")'
    dlog "gorev $gid BASARISIZ (rapor yazari: plan zinciri tukendi, G-R6)"; echo basarisiz; return
  fi
  if [ -n "$mdl" ]; then
    local alt
    for alt in $(diger_provider "$mdl" "$gid"); do   # B281: not a single alternative; the next providers of the chain (free ones included)
      dlog "gorev $gid yarim ($mdl) -> diger provider: $alt"
      d=$(kosu_dogrulu "$gid" "$alt"); [ "$d" = "uretti" ] || [ "$d" = "iptal" ] || arac_bekliyor && { echo "$d"; return; }
      # B331: the failure record is written to the model that fell (diger_provider only records the first model)
      python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" butce-reddi "$CADRE_RUN_ID" "${gid}" "${alt%%/*}" >/dev/null 2>&1 \
        || python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ariza "$alt" "gorev yarim" >/dev/null 2>&1
    done
    dlog "gorev $gid: bulut zinciri tukendi"
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" son-deneme-butce "$CADRE_RUN_ID" "$gid" 2>/dev/null && CADRE_GOREV_ID="$gid" python3 -c 'import os,sys; sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib"); from qe_core import butce_bekleme_kaydet; from pathlib import Path; butce_bekleme_kaydet(Path(os.environ["CADRE_RUN_DIR"])/"dispatch.log")'

  fi
  dlog "gorev $gid BASARISIZ (zincir tukendi)"
  echo basarisiz
}

serit_modeli(){ # $1=id -> the model given to the lane (empty=the router chooses); the plan model goes through the budget/difficulty preflight
  local gm gmdl gz gmdl2=""
  gm=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" model "$1")
  gz=$(printf '%s' "$plan_json" | GID="$1" python3 -c "import json,sys,os;print([g.get('zorluk',3) for g in json.load(sys.stdin)['gorevler'] if g['id']==os.environ['GID']][0])")
  # the plan model goes through the difficulty/budget preflight (yonlendirici.zorluk_modeli)
  if [ -n "$gm" ] && [ "$gm" != auto ]; then gmdl="$(model_map "$gm")"; else gmdl=""; fi
  gmdl2="$(CADRE_GOREV_ID="$1" python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" uygun "$gmdl" impl "$gz" 2>/dev/null)"   # B130: the old `sir` argument was removed
  [ "$gmdl2" = "$gmdl" ] || dlog "gorev $1: $gm (${gmdl:-auto}) -> ${gmdl2:-auto} (zorluk=$gz/butce)"
  printf '%s' "$gmdl2"
}

rapor_yazar_kos(){ # G-R6: $1=id -> uretti|basarisiz|iptal; the lane that writes the report runs on the strong model of the plan chain
  local gid="$1" m
  m="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk yazar orta 30000 2>/dev/null)"   # B239: plan chain + breathing filter
  if [ -z "$m" ] && bulut_bekle plan orta "${SEV_PUAN:-0}"; then m="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk yazar orta 30000 2>/dev/null)"; fi
  if [ -z "$m" ]; then
    CADRE_GOREV_ID="$gid" python3 -c 'import os,sys; sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib"); from qe_core import butce_bekleme_kaydet; from pathlib import Path; butce_bekleme_kaydet(Path(os.environ["CADRE_RUN_DIR"])/"dispatch.log")'
    dlog "gorev $gid BASARISIZ (rapor yazari: guclu model yok, G-R6)"; echo basarisiz; return
  fi
  dlog "gorev $gid: rapor yazari (G-R6) -> $m"
  local d; d=$(gorev_kos_zincirli "$gid" "$m" rapor)
  if [ "$d" = uretti ]; then
    case "${SEV_AD:-}" in   # B226: while extra lanes are running, the lane pocket is not handed to the judge (butce_kapisi.seritler_bitti)
      zor|cok-zor|ultra) : > "$Z/ek-serit.flag"; rapor_mutabakat "$gid" "$m"; rm -f "$Z/ek-serit.flag";;
      *) python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" analist-kaydi tek "$WT" "$m" "" "" "zorluk mutabakat esiginin altinda (G-R7 yalniz zor+)" 2>>"$DLOG";;
    esac
  fi
  echo "$d"
}


rapor_mutabakat(){ # G-R7: $1=id $2=first analyst; in a hard+ report the second analyst writes without seeing the first draft, a third model reconciles (no stdout)
  local gid="$1" m1="$2" m2 m3 ry ky A="$Z/analist-a" B="$Z/analist-b" GP d
  # B258: on re-approval (--devam/retry), if the reconciler already produced or was discarded, G-R7 does not run again (~$0.3 + 5 min).
  if [ "$(task_state "$gid-mutabakat" 2>/dev/null)" = uretti ] || [ -f "$(task_dir "$gid-mutabakat")/atildi.flag" ]; then
    dlog "G-R7: mutabakat zaten sonuclanmis — ek seritler atlandi (B258)"; return 0
  fi
  read -r ry ky <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" yollar 2>>"$DLOG")"
  m2="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" sonraki plan "$m1" 2>/dev/null)"
  if [ -z "$ry" ] || [ -z "$m2" ] || [ "$m2" = "$m1" ]; then
    dlog "G-R7: ikinci guclu analist yok -> tek analist ($m1)"
    python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" analist-kaydi tek "$WT" "$m1" "" "" "ikinci guclu analist yok" 2>>"$DLOG"
    return 0
  fi
  mkdir -p "$A" "$B" || return 0
  mv -f "$WT/$ry" "$A/" 2>>"$DLOG"; [ -f "$WT/$ky" ] && mv -f "$WT/$ky" "$A/" 2>>"$DLOG"   # B cannot see the first draft
  GP="$(task_dir "$gid-analist-b")/prompt.md" && cat "$(gorev_pf "$gid")" > "$GP" \
    && printf '\n%s\n' "$(python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" analist-istemi 2>>"$DLOG")" >> "$GP" || GP=""
  dlog "G-R7: ikinci bagimsiz analist -> $m2"
  d="$( [ -n "$GP" ] && ek_serit_kos "$gid-analist-b" "$m2" "$GP")"
  if [ "$d" != uretti ] || [ ! -s "$WT/$ry" ]; then
    dlog "G-R7: ikinci analist uretemedi (${d:-istem yok}) -> ilk analistin raporu"; cp -f "$A/"* "$(dirname "$WT/$ry")/" 2>>"$DLOG"
    : > "$(task_dir "$gid-analist-b")/atildi.flag"   # B256: a lane whose output was discarded does not count as a writer (judge pool)
    python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" analist-kaydi tek "$WT" "$m1" "$m2" "" "ikinci analist uretemedi" 2>>"$DLOG"
    return 0
  fi
  mv -f "$WT/$ry" "$B/" 2>>"$DLOG"; [ -f "$WT/$ky" ] && mv -f "$WT/$ky" "$B/" 2>>"$DLOG"
  mkdir -p "$WT/.cadre-plan/analist-a" "$WT/.cadre-plan/analist-b" && cp -f "$A/"* "$WT/.cadre-plan/analist-a/" && cp -f "$B/"* "$WT/.cadre-plan/analist-b/"
  m3="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" sonraki plan "$m2" 2>/dev/null)"; [ -n "$m3" ] || m3="$m1"
  GP="$(task_dir "$gid-mutabakat")/prompt.md" && { python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" mutabakat-istemi 2>>"$DLOG"; echo; cat "$(gorev_pf "$gid")"; } > "$GP"
  dlog "G-R7: mutabakat -> $m3"
  d="$(ek_serit_kos "$gid-mutabakat" "$m3" "$GP")"
  if [ "$d" != uretti ] || [ ! -s "$WT/$ry" ]; then
    dlog "G-R7: mutabakat uretemedi (${d}) -> ilk analistin raporu"; cp -f "$A/"* "$(dirname "$WT/$ry")/" 2>>"$DLOG"
    : > "$(task_dir "$gid-analist-b")/atildi.flag"; : > "$(task_dir "$gid-mutabakat")/atildi.flag"   # B256
    python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" analist-kaydi tek "$WT" "$m1" "$m2" "$m3" "mutabakat uretemedi" 2>>"$DLOG"
    return 0
  fi
  python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" analist-kaydi mutabakat "$WT" "$m1" "$m2" "$m3" "" 2>>"$DLOG"
  return 0
}

gorev_bol(){ # G-E10: $1=id -> uretti|basarisiz|iptal; a lane whose chain is exhausted is split once into 2-3 sequential sub-tasks (bolme.py)
  local gid="$1" ids sid r bm bp raw
  ids="$(python3 "$CADRE_ENGINE_ROOT/lib/bolme.py" alt "$CADRE_RUN_ID" "$gid" 2>>"$DLOG")"
  if [ -z "$ids" ]; then
    bm="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk plan zor 30000 2>/dev/null)" || { dlog "gorev $gid: bolme icin planlayici yok"; echo basarisiz; return; }
    [ -n "$bm" ] || { dlog "bolme: bulut kapasitesi yok"; echo basarisiz; return; }
    bp="$(task_dir "bol-$gid")" && mkdir -p "$bp/sandbox" || { echo basarisiz; return; }
    dlog "gorev $gid BOLUNUYOR (G-E10, seviye ${SEV_AD:-?}, planlayici $bm)"
    raw="$(planlayici_cagir "$bm" "$(python3 "$CADRE_ENGINE_ROOT/lib/bolme.py" istem "$CADRE_RUN_ID" "$gid" 2>>"$DLOG")" "bol-$gid" "$bp/sandbox" 4 bolme)" || { echo basarisiz; return; }
    ids="$(printf '%s' "$raw" | python3 "$CADRE_ENGINE_ROOT/lib/bolme.py" birlestir "$CADRE_RUN_ID" "$gid" "$WT" 2>>"$DLOG")" || ids=""
    [ -n "$ids" ] || { dlog "gorev $gid: alt plan gecersiz ya da bos -> bolme yok"; echo basarisiz; return; }
    plan_json="$(cat "$Z/plan.json")"
    dlog "gorev $gid alt gorevler: $ids"
  fi
  for sid in $ids; do
    [ -f "$Z/cancel.flag" ] && { echo iptal; return; }
    r=$(gorev_kos_zincirli "$sid" "$(serit_modeli "$sid")")
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-result "$CADRE_RUN_ID" "$sid" "$r" >/dev/null || { echo basarisiz; return; }
    if [ "$r" != uretti ]; then
      dlog "gorev $gid: alt gorev $sid $r -> bolme basarisiz"
      if [ "$r" = iptal ]; then echo iptal; else echo basarisiz; fi
      return
    fi
  done
  echo uretti
}

paralel_go(){ # TASARIM-5(3): $1=model $2=previous pairs; if the same small provider is needed a second time in parallel, Go if there is a Go share (B100 was relaxed for this)
  local m="$1" go
  case "$m" in opencode-go/*|claude/*) printf '%s' "$m"; return;; esac
  case " $2" in *"=${m%%/*}/"*) ;; *) printf '%s' "$m"; return;; esac
  go="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" uygun opencode-go/deepseek-v4-pro impl 3 2>/dev/null)"
  if [ "$go" = opencode-go/deepseek-v4-pro ]; then dlog "PARALEL: ${m%%/*} ikinci kez -> Go ($go)"; printf '%s' "$go"; else printf '%s' "$m"; fi
}

paralel_kos(){ # TASARIM-5(2): $@=id=model; each lane in the background with its own threshold chain, result in $Z/.paralel-<id>; all are waited for
  local c pidler=""
  for c in "$@"; do
    ( gorev_kos_zincirli "${c%%=*}" "${c#*=}" > "$Z/.paralel-${c%%=*}" ) &
    pidler="$pidler $!"
  done
  for c in $pidler; do wait "$c"; done
}

# One worktree: dependent lanes run serially; independent cloud lanes (TASARIM-5) run concurrently when their files are disjoint.
PARALEL="${CADRE_PARALEL_SERIT:-2}"
PARALEL_BITEN=" "
SONUCLAR=""
HEPSI_URETTI=1
for gid in $GIDS; do
  [ -f "$Z/cancel.flag" ] && { dlog "IS IPTAL (bayrak)"; exit 130; }
  case "$PARALEL_BITEN" in *" $gid "*) continue;; esac
  ready=1
  needs=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" bagimli "$gid") || exit 1
  for dep in $needs; do
    ds=$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-result "$CADRE_RUN_ID" "$dep") || exit 1
    [ "$ds" = uretti ] || ready=0
  done
  if [ "$ready" = 0 ]; then
    r=bloke
    python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task "$CADRE_RUN_ID" "$gid" bloke || exit 1
    dlog "gorev $gid: onceki bagimlilik basarisiz; baslatilmadi"
  elif [ -n "$RAPOR_KIPI" ] && python3 "$CADRE_ENGINE_ROOT/lib/rapor.py" yazar "$gid" 2>>"$DLOG"; then
    r=$(rapor_yazar_kos "$gid")
  elif [ -n "$(python3 "$CADRE_ENGINE_ROOT/lib/bolme.py" alt "$CADRE_RUN_ID" "$gid" 2>>"$DLOG")" ]; then
    r=$(gorev_bol "$gid")   # G-E10: a task split in an earlier attempt continues with its sub-tasks
  else
    gm="$(serit_modeli "$gid")"; ciftler="$gid=$gm"
    if [ -n "$gm" ] && [ "$PARALEL" -gt 1 ] 2>/dev/null; then
      for og in $(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" paralel "$gid" "$PARALEL" 2>>"$DLOG"); do
        om="$(serit_modeli "$og")"; [ -n "$om" ] && ciftler="$ciftler $og=$(paralel_go "$om" "$ciftler")"
      done
    fi
    [ "$ciftler" = "$gid=$gm" ] || { dlog "PARALEL (TASARIM-5): $ciftler"; paralel_kos $ciftler; }
    for c in $ciftler; do
      og="${c%%=*}"
      if [ "$ciftler" = "$gid=$gm" ]; then ro=$(gorev_kos_zincirli "$gid" "$gm"); else ro="$(cat "$Z/.paralel-$og" 2>/dev/null)"; ro="${ro:-basarisiz}"; fi
      if [ "$ro" = basarisiz ] && python3 "$CADRE_ENGINE_ROOT/lib/bolme.py" bolunebilir "$CADRE_RUN_ID" "$og" "${SEV_AD:-}" >/dev/null 2>>"$DLOG"; then
        ro=$(gorev_bol "$og")
      fi
      if [ "$og" = "$gid" ]; then r="$ro"; continue; fi
      python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-result "$CADRE_RUN_ID" "$og" "$ro" || exit 1
      SONUCLAR="$SONUCLAR $og=$ro"; PARALEL_BITEN="$PARALEL_BITEN$og "
      [ "$ro" = uretti ] || HEPSI_URETTI=0
    done
  fi
  python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task-result "$CADRE_RUN_ID" "$gid" "$r" || exit 1
  SONUCLAR="$SONUCLAR $gid=$r"
  [ "$r" = uretti ] || HEPSI_URETTI=0
done
dlog "gorev sonuclari:$SONUCLAR"
[ -f "$Z/cancel.flag" ] && { dlog "IS IPTAL (bayrak)"; exit 130; }
[ "$HEPSI_URETTI" = 1 ] || { dlog "bagimli/eksik gorev var; urun korunuyor, birlestirme baslatilmadi"; exit 1; }

# --- MERGE + QUALITY ---
# LIGHT PATH: on a small job (<=3 tasks) the LLM merge is SKIPPED;
# on a large job the merge uses the cloud chain of the code-writing role.
KMODEL=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" kalite_model)
KM=$(model_map "$KMODEL")
KM_Y="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk kalite orta 30000 2>/dev/null)"; [ -n "$KM_Y" ] && { KM="$KM_Y"; KMODEL="yonlendirici"; }   # Evre 10
GSAYI=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" gids | wc -w | tr -d ' ')
if [ "$GSAYI" -le 3 ] || [ -n "$RAPOR_KIPI" ]; then   # a report job has no code to merge
  dlog "HAFIF BIRLESTIRME: $GSAYI gorev, hepsi uretti — LLM'siz dogrulama"
  # C28: commit only through qe_commit_wip with the shared EXC list (harness files never enter the product branch); no raw `git add -A`.
  n_commit="$(qe_commit_wip "$WT" "$proje" "dispatch: gorev sonuclari" 2>>"$DLOG")"; dlog "birlestirme commit: ${n_commit:-0} dosya"
  ( cd "$WT" && echo "== diff ozeti:"; git diff --stat HEAD~1 2>/dev/null | tail -5 ) >>"$DLOG" 2>&1
  B1="uretti"
else
BP="$(task_dir birlestirme)/prompt.md"
{ printf 'BIRLESTIRME GOREVI: git log/diff ile bu worktree'"'"'deki TUM gorev degisikliklerini sirayla gozden gecir (%s).\n' \
    "$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" sira)"
  printf 'KISITLAR: en fazla 5 eylem; derin analiz YOK; testler varsa SADECE kos; celiski/kirik yoksa 3 satirlik ozet yazip HEMEN DUR.\n'
  if [ -n "$KM" ]; then printf 'NOT: kalite incelemesi ayri yapilacak, sen sadece birlestir+test.\n'; else printf 'KALITE (oz-denetim): degisikligin sinir durumlarini yaz, testleri kos, 3 maddelik kontrol listesi doldur.\n'; fi
} > "$BP"
BMODEL="$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" ilk birlestirme orta 30000 2>/dev/null)"   # Evre 10
[ -n "$BMODEL" ] || { dlog "birlestirme: bulut kapasitesi bekleniyor"; exit 1; }
dlog "birlestirme modeli: $BMODEL"
B1=$(env CADRE_WORKTREE="$WT" CADRE_GOREV_ID="birlestirme" CADRE_RUN_MODEL="$BMODEL" bash "$CADRE_ENGINE_ROOT/bin/is-motoru.sh" "$proje" "$BP" >>"$DLOG" 2>&1 && awk '{print $1}' "$(task_dir birlestirme)/status.txt" 2>/dev/null)
if [ "${B1:-yarim}" != "uretti" ]; then
  dlog "birlestirme yarim -> 2. deneme"
  B1=$(env CADRE_WORKTREE="$WT" CADRE_GOREV_ID="birlestirme" CADRE_RUN_MODEL="$BMODEL" bash "$CADRE_ENGINE_ROOT/bin/is-motoru.sh" "$proje" "$BP" >>"$DLOG" 2>&1 && awk '{print $1}' "$(task_dir birlestirme)/status.txt" 2>/dev/null)
  if [ "${B1:-yarim}" != "uretti" ] && [ -n "$KM$KMODEL" ]; then
    ALT=$(python3 "$CADRE_ENGINE_ROOT/lib/yonlendirici.py" alternatifler birlestirme "$BMODEL" 1 2>/dev/null | head -1)
    if [ -n "$ALT" ]; then
      dlog "BIRLESTIRME TIKANDI -> zincirdeki sonraki model: $ALT"
      B1=$(env CADRE_WORKTREE="$WT" CADRE_GOREV_ID="birlestirme" CADRE_RUN_MODEL="$ALT" bash "$CADRE_ENGINE_ROOT/bin/is-motoru.sh" "$proje" "$BP" >>"$DLOG" 2>&1 && awk '{print $1}' "$(task_dir birlestirme)/status.txt" 2>/dev/null)
    else
      dlog "BIRLESTIRME TIKANDI -> zincirde baska model yok"
    fi
  fi
fi
fi
# One final predicate owns QA + acceptance + quality for both execution paths.
if [ "${B1:-yarim}" != "uretti" ]; then
  dlog "DISPATCH IS YARIM (birlestirme=$B1). worktree=$WT"; exit 1
fi
duzeltme_dongusu; exit $?
