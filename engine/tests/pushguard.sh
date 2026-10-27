#!/usr/bin/env bash
# Live test of the pre-push guard — the main safety barrier between the model
# and the user's real branches. Runs entirely in disposable repos.
set -uo pipefail
exec </dev/null
export GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/usr/bin/true
QE="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SP="$(mktemp -d "${TMPDIR:-/tmp}/qe-pushguard.XXXXXX")" || exit 1
export CADRE_HOME="$SP/data"
mkdir -p "$CADRE_HOME/logs"
P=0; F=0
ok(){ P=$((P+1)); printf '  PASS  %s\n' "$1"; }
no(){ F=$((F+1)); printf '  FAIL  %s\n     -> %s\n' "$1" "${2:-}"; }

mkdir -p "$SP"
git init --bare -q "$SP/remote.git"
git init -q "$SP/repo"
cd "$SP/repo" || exit 1
git config user.email t@t; git config user.name t
git config core.hooksPath "$QE/hooks"
git remote add origin "$SP/remote.git"
echo one > a.txt; git add a.txt; git commit -qm init
git branch -M main
git push -q origin main 2>/dev/null   # seed the remote (guard should deny; seed via direct path)
git --git-dir="$SP/remote.git" symbolic-ref HEAD refs/heads/main 2>/dev/null

echo "== PUSH GUARD CANLI TESTI =="

# 1. a cadre/* branch must be pushable
git checkout -qb cadre/test-1
echo two >> a.txt; git commit -qam "cadre work"
if git push -q origin cadre/test-1 2>/dev/null; then ok "1 cadre/* dali push EDILEBILIR"
else no "1 cadre/* push" "reddedildi (izin verilmeliydi)"; fi

# 2. the canonical branch (main) must not be pushable
git checkout -q main 2>/dev/null
echo three >> a.txt; git commit -qam "main work"
if git push -q origin main 2>&1 | grep -qi 'deny\|reject\|refus'; then ok "2 main push REDDEDILDI"
elif git push -q origin main 2>/dev/null; then no "2 main push" "IZIN VERILDI — GUVENLIK ACIGI"
else ok "2 main push REDDEDILDI"; fi

# 3. a REAL destructive push (history rewritten) must be blocked.
# NOTE: forcing a fast-forwardable branch is harmless and allowed; the dangerous
# one is a push that DROPS a remote commit — the test has to produce that.
git checkout -q cadre/test-1
git commit -q --amend -m "tarih yeniden yazildi"
if git push -q --force origin cadre/test-1 2>/dev/null; then no "3 tarih-yeniden-yazan push" "IZIN VERILDI — GUVENLIK ACIGI"
else ok "3 tarih-yeniden-yazan push REDDEDILDI"; fi
# 3b. false-positive check: a fast-forward on a CLEAN branch must still work.
# (test-1 was amended, so it now diverges — it cannot be measured with it.)
git checkout -q -b cadre/test-2 2>/dev/null
git commit -q --allow-empty -m "temiz dal"
git push -q origin cadre/test-2 2>/dev/null || true
echo four >> a.txt; git commit -qam "ileri sarma"
if git push -q origin cadre/test-2 2>/dev/null; then ok "3b zararsiz ileri-sarma push EDILEBILIR"
else no "3b ileri-sarma push" "reddedildi (izin verilmeliydi)"; fi

# 4. a push to a foreign target (exfil) must be blocked
git init --bare -q "$SP/foreign.git"
if git push -q "$SP/foreign.git" cadre/test-1 2>/dev/null; then no "4 yabanci remote" "IZIN VERILDI — EXFIL RISKI"
else ok "4 yabanci remote'a push REDDEDILDI"; fi

# 5. tags must not be pushable
git tag v1 2>/dev/null
if git push -q origin v1 2>/dev/null; then no "5 tag push" "IZIN VERILDI"
else ok "5 tag push REDDEDILDI"; fi

# 6. branch deletion must be blocked
if git push -q origin :cadre/test-1 2>/dev/null; then no "6 uzak dal silme" "IZIN VERILDI"
else ok "6 uzak dal silme REDDEDILDI"; fi

# cleanup
cd /; find "$SP" -type f -delete 2>/dev/null; find "$SP" -type d -depth -exec rmdir {} + 2>/dev/null
printf '\nPUSH GUARD: %d PASS, %d FAIL\n' "$P" "$F"
[ "$F" -eq 0 ]
