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

echo "== PUSH GUARD LIVE TEST =="

# 1. a cadre/* branch must be pushable
git checkout -qb cadre/test-1
echo two >> a.txt; git commit -qam "cadre work"
if git push -q origin cadre/test-1 2>/dev/null; then ok "1 cadre/* branch CAN be pushed"
else no "1 cadre/* push" "denied (should have been allowed)"; fi

# 2. the canonical branch (main) must not be pushable
git checkout -q main 2>/dev/null
echo three >> a.txt; git commit -qam "main work"
if git push -q origin main 2>&1 | grep -qi 'deny\|reject\|refus'; then ok "2 main push DENIED"
elif git push -q origin main 2>/dev/null; then no "2 main push" "ALLOWED — SECURITY HOLE"
else ok "2 main push DENIED"; fi

# 3. a REAL destructive push (history rewritten) must be blocked.
# NOTE: forcing a fast-forwardable branch is harmless and allowed; the dangerous
# one is a push that DROPS a remote commit — the test has to produce that.
git checkout -q cadre/test-1
git commit -q --amend -m "history rewritten"
if git push -q --force origin cadre/test-1 2>/dev/null; then no "3 history-rewriting push" "ALLOWED — SECURITY HOLE"
else ok "3 history-rewriting push DENIED"; fi
# 3b. false-positive check: a fast-forward on a CLEAN branch must still work.
# (test-1 was amended, so it now diverges — it cannot be measured with it.)
git checkout -q -b cadre/test-2 2>/dev/null
git commit -q --allow-empty -m "clean branch"
git push -q origin cadre/test-2 2>/dev/null || true
echo four >> a.txt; git commit -qam "fast-forward"
if git push -q origin cadre/test-2 2>/dev/null; then ok "3b harmless fast-forward push CAN be pushed"
else no "3b fast-forward push" "denied (should have been allowed)"; fi

# 4. a push to a foreign target (exfil) must be blocked
git init --bare -q "$SP/foreign.git"
if git push -q "$SP/foreign.git" cadre/test-1 2>/dev/null; then no "4 foreign remote" "ALLOWED — EXFIL RISK"
else ok "4 push to a foreign remote DENIED"; fi

# 5. tags must not be pushable
git tag v1 2>/dev/null
if git push -q origin v1 2>/dev/null; then no "5 tag push" "ALLOWED"
else ok "5 tag push DENIED"; fi

# 6. branch deletion must be blocked
if git push -q origin :cadre/test-1 2>/dev/null; then no "6 remote branch deletion" "ALLOWED"
else ok "6 remote branch deletion DENIED"; fi

# cleanup
cd /; find "$SP" -type f -delete 2>/dev/null; find "$SP" -type d -depth -exec rmdir {} + 2>/dev/null
printf '\nPUSH GUARD: %d PASS, %d FAIL\n' "$P" "$F"
[ "$F" -eq 0 ]
