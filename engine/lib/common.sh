#!/usr/bin/env bash
# Shared helpers for the cadre launcher. Sourced, not executed.
# Central layout: ~/.local/share/cadre-engineering/{projects,worktrees,logs,reports,hooks}

CADRE_HOME="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"
export CADRE_HOME
CADRE_PROJECTS="$CADRE_HOME/projects"
CADRE_WORKTREES="$CADRE_HOME/worktrees"
CADRE_LOGS="$CADRE_HOME/logs"
CADRE_REPORTS="$CADRE_HOME/reports"
CADRE_HOOKS="$CADRE_HOME/hooks"

qe_bekci_devam() {
  [ -d "$CADRE_HOME" ] && [ -d "$CADRE_ENGINE_ROOT" ] || return 1
  [ -z "${PIDF:-}" ] || [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] || return 1
  [ -z "${CADRE_BEKCI_SAHIP_PID:-}" ] && return 0
  python3 - "$CADRE_BEKCI_SAHIP_PID" "${CADRE_BEKCI_SAHIP_KIMLIK:-}" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import process_identity
try:
    expected = json.loads(sys.argv[2])
    actual = process_identity(int(sys.argv[1]))
    raise SystemExit(0 if actual and actual == expected else 1)
except (ValueError, TypeError):
    raise SystemExit(1)
PY
}

qe_bekci_temizle() {
  trap - EXIT TERM INT HUP
  python3 - "$$" <<'PY' 2>/dev/null
import os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from qe_core import _cocuklar, kill_tree
from kosu import process_identity
parent = int(sys.argv[1])
children = {pid: process_identity(pid) for pid in _cocuklar(parent) if pid != os.getpid()}
for pid, identity in children.items():
    if identity and process_identity(pid) == identity:
        kill_tree(pid, grace=1)
PY
  wait 2>/dev/null || true
}

qe_bekci_omur() {
  if [ -n "${CADRE_BEKCI_SAHIP_PID:-}" ] && [ -z "${CADRE_BEKCI_SAHIP_KIMLIK:-}" ]; then
    CADRE_BEKCI_SAHIP_KIMLIK="$(python3 - "$CADRE_BEKCI_SAHIP_PID" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import process_identity
print(json.dumps(process_identity(int(sys.argv[1]))))
PY
)" || return 1
    export CADRE_BEKCI_SAHIP_KIMLIK
  fi
  qe_bekci_devam || return 1
  trap 'qe_bekci_temizle' EXIT
  trap 'exit 0' TERM INT HUP
}

qe_err() { printf 'cadre: %s\n' "$*" >&2; }
qe_die() { qe_err "$*"; exit 1; }

qe_require() {
  command -v "$1" >/dev/null 2>&1 || qe_die "required tool missing: $1"
}

qe_repo_root() { git rev-parse --show-toplevel 2>/dev/null || true; }

# Print project name for a directory (configured repo root or AI worktree).
# Returns 1 if the directory is not a configured project.
qe_project_for_dir() {
  local dir="$1" p rp
  case "$dir" in
    "$CADRE_WORKTREES"/*)
      p="${dir#"$CADRE_WORKTREES"/}"; p="${p%%/*}"
      if [ -n "$p" ] && [ -d "$CADRE_PROJECTS/$p" ]; then printf '%s\n' "$p"; return 0; fi
      ;;
  esac
  # B270: project-mode sub-projects (`ad--p<id>`) carry the same repo path and come first in glob order;
  # the shortest name is the real project and wins (previously the first match, a stale sub-project, was picked and runs nested sub-projects).
  local best=""
  for p in "$CADRE_PROJECTS"/*/; do
    [ -f "${p}repo-path" ] || continue
    rp="$(cat "${p}repo-path")"
    [ "$rp" = "$dir" ] || continue
    p="$(basename "$p")"
    if [ -z "$best" ] || [ "${#p}" -lt "${#best}" ]; then best="$p"; fi
  done
  [ -n "$best" ] && { printf '%s\n' "$best"; return 0; }
  return 1
}

qe_repo_path()   { cat "$CADRE_PROJECTS/$1/repo-path" 2>/dev/null; }
qe_base_branch() { cat "$CADRE_PROJECTS/$1/base-branch" 2>/dev/null; }

# Compose OPENCODE_CONFIG_CONTENT for a project. Role: default | night.
# Fails closed: any jq error or empty result aborts the launcher rather than
# silently handing the agent a config with no memory / no hardening.
qe_config_content() {
  local project="$1" role="${2:-default}" pdir base f
  pdir="$CADRE_PROJECTS/$project"
  local files=()
  for f in PROJECT.md POLICY.md STATE.md; do
    [ -f "$pdir/$f" ] && files+=("$pdir/$f")
  done
  if [ "${#files[@]}" -gt 0 ]; then
    base="$(printf '%s\n' "${files[@]}" | jq -R . | jq -cs '{instructions: .}')" \
      || qe_die "failed to build instructions JSON for $project"
  else
    base='{}'
  fi
  if [ "$role" = "night" ]; then
    if [ -f "$pdir/permissions.night.json" ]; then
      base="$(printf '%s\n%s\n' "$base" "$(cat "$pdir/permissions.night.json")" | jq -cs '.[0] * .[1]')" \
        || qe_die "invalid permissions.night.json for $project (fix or remove it)"
    fi
  fi
  # Per-project MCP. Servers may ship disabled: tool schemas cost every session (one server ~16.5k tokens).
  # Opt in per run with CADRE_MCP_ENABLE="name[,name2]" when the task needs that server.
  if [ -f "$pdir/mcp.json" ]; then
    local mcpjson
    mcpjson="$(jq -c '{mcp: .}' "$pdir/mcp.json")" || qe_die "invalid mcp.json for $project"
    # CADRE_MCP_OFF: methodology/code tasks do not pay the schema cost (one MCP server ~8k tokens/session).
    if [ -n "${CADRE_MCP_OFF:-}" ]; then
      mcpjson="$(printf '%s' "$mcpjson" | jq -c '.mcp |= with_entries(.value.enabled = false)')" \
        || qe_die "failed to disable mcp"
    fi
    if [ -n "${CADRE_MCP_ENABLE:-}" ]; then
      mcpjson="$(printf '%s' "$mcpjson" \
        | jq -c --arg on "$CADRE_MCP_ENABLE" '
            ($on | split(",") | map(gsub("^\\s+|\\s+$";""))) as $names
            | .mcp |= with_entries(if (.key | IN($names[])) then .value.enabled = true else . end)')" \
        || qe_die "failed to apply CADRE_MCP_ENABLE"
    fi
    base="$(printf '%s\n%s\n' "$base" "$mcpjson" | jq -cs '.[0] * .[1]')" \
      || qe_die "failed to merge mcp config for $project"
  fi
  [ -n "$base" ] && [ "$base" != "null" ] || qe_die "empty config content for $project"
  printf '%s' "$base"
}

# Common environment for every Cadre launcher: no Claude Code inheritance.
qe_export_env() {
  export OPENCODE_DISABLE_CLAUDE_CODE=1
}

# Read a value from the global .jsonc by JQ PATH; strips whole-line // comments only (inline http:// is safe),
# then jq makes the result path-precise and independent of field order / duplicate keys.
qe_cfg_field() {
  local cfg="$HOME/.config/opencode/opencode.jsonc"
  sed -E 's@^[[:space:]]*//.*$@@' "$cfg" 2>/dev/null | jq -r "${1} // empty" 2>/dev/null
}

# Commit whatever is uncommitted in an AI worktree, so a killed/resumed run never loses in-flight edits.
# Refuses unless the branch is cadre/* and the worktree is under the central worktree area.
qe_commit_wip() {
  # Harness memory (.gorevler.json / .veri-defteri.md / STATE) is not a deliverable and is not committed.
  # C28: ONE EXC list — plan ledgers (.cadre-plan/) and QA/autopsy leftovers do NOT enter the product branch either; the dispatcher uses this function too.
  # B16: QA/verification leftovers are not product
  # B175-b: `add -A -- . :(exclude,glob)var/...` named the ignored `var/` directory, so git returned rc=1 ->
  # no commit -> delivery-commit-missing. Everything is added first (`.` silently skips ignored paths), then harness paths are removed from the index
  # (`reset -- path`: an untracked new file drops out of the index, a tracked file returns to HEAD; a non-matching path is not an error).
  [ "${CADRE_NO_AUTO_COMMIT:-0}" != 1 ] || return 0
  [ -n "${CADRE_COMMIT_ACTION:-}" ] || return 0
  [ -n "${CADRE_RUN_ID:-}" ] || return 78
  python3 "$CADRE_ENGINE_ROOT/lib/izin.py" checkpoint "$CADRE_RUN_ID" "$CADRE_COMMIT_ACTION" "$1" "$2"
}

qe_log_session() {
  # qe_log_session <event> <project> <agent> <dir> [extra]
  mkdir -p "$CADRE_LOGS"
  printf '%s %s project=%s agent=%s dir=%s %s\n' \
    "$(date '+%F %T')" "$1" "${2:--}" "${3:--}" "${4:--}" "${5:-}" \
    >> "$CADRE_LOGS/sessions.log" 2>/dev/null || true
}

# Create an isolated cadre/* worktree. Prints the worktree path on stdout.
qe_make_worktree() {
  local project="$1" task="$2"
  local repo base slug branch id wt
  repo="$(qe_repo_path "$project")"
  [ -n "$repo" ] || qe_die "no repo-path configured for project '$project'"
  [ -e "$repo/.git" ] || qe_die "repo not found: $repo"
  # A run has one deterministic worktree. Never turn an interrupted setup into
  # a second worktree with a PID suffix; keep the first one for reconciliation.
  # C16: in a recorded run the source is pinned AT ACCEPTANCE TIME (runs/<id>/source): SHA + uncommitted patch + untracked copies.
  local src_sha="" src_branch="" src_patch="-" src_untracked="-" src_carried=0 start_ref=""
  if [ -n "${CADRE_RUN_ID:-}" ]; then
    case "$CADRE_RUN_ID" in *[!a-zA-Z0-9_-]*|"") qe_die "invalid run id";; esac
    wt="$CADRE_WORKTREES/$project/$CADRE_RUN_ID"
    if [ -e "$wt" ]; then
      [ -f "$CADRE_RUN_DIR/worktree-ready" ] || qe_die "incomplete worktree setup retained: $wt"
      qe_verify_ai_worktree "$wt" "$project" || return 1
      printf '%s\n' "$wt"
      return 0
    fi
    local src_info
    src_info="$(python3 "${CADRE_ENGINE_ROOT:-$CADRE_HOME}/lib/kosu.py" source-info "$CADRE_RUN_ID")" || qe_die "accepted source record unavailable for $CADRE_RUN_ID"
    read -r src_sha src_branch src_patch src_untracked src_carried <<< "$src_info"
    [ "$src_sha" != "-" ] || src_sha=""
  fi
  # ACTIVE BRANCH (user decision): the worktree opens from the branch at the MOMENT `opencode` was typed
  # — whatever state the user sees. projects/<p>/base-branch is only a FALLBACK:
  # it applies on a detached HEAD or while on a cadre/* branch. CADRE_BASE_BRANCH forces it.
  local cur; cur="$(git -C "$repo" branch --show-current 2>/dev/null)"
  base="${CADRE_BASE_BRANCH:-}"
  if [ -z "$base" ]; then
    case "$cur" in ""|cadre/*) base="$(qe_base_branch "$project")" ;; *) base="$cur" ;; esac
  fi
  if [ -n "$src_sha" ]; then
    # The accepted SHA is authoritative: even if the user committed in the meantime, plan and implementation see the same source.
    git -C "$repo" cat-file -e "$src_sha^{commit}" 2>/dev/null || qe_die "accepted source $src_sha is not a commit in $repo"
    start_ref="$src_sha"; [ -n "$src_branch" ] && [ "$src_branch" != "-" ] && base="$src_branch"
  else
    [ -n "$base" ] || qe_die "no base-branch configured for '$project'"
    git -C "$repo" show-ref --verify --quiet "refs/heads/$base" \
      || qe_die "base branch '$base' does not exist locally in $repo"
    start_ref="refs/heads/$base"
  fi
  # extensions.worktreeConfig would leak an existing core.worktree to every
  # linked worktree — refuse rather than corrupt path resolution.
  if git -C "$repo" config --get core.worktree >/dev/null 2>&1; then
    qe_die "repo sets core.worktree; enabling per-worktree hooks is unsafe here"
  fi

  # Flatten to ONE line before slugging: sed/cut work per line, so a multi-line task gave an invalid branch name.
  slug="$(printf '%s' "$task" | tr '\n\r\t' '   ' | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-32)"
  [ -n "$slug" ] || slug="task"
  id="$(date +%Y%m%d-%H%M%S)-$slug"
  [ -z "${CADRE_RUN_ID:-}" ] || id="$CADRE_RUN_ID"
  branch="cadre/$id"
  wt="$CADRE_WORKTREES/$project/$id"
  # Two openings in the same second (parallel dispatchers) collide on the same name ("worktree could not be opened"); on collision, a pid suffix.
  if [ -e "$wt" ] || git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
    [ -z "${CADRE_RUN_ID:-}" ] || qe_die "incomplete run branch retained: $branch"
    id="$id-$$"; branch="cadre/$id"; wt="$CADRE_WORKTREES/$project/$id"
  fi

  mkdir -p "$CADRE_WORKTREES/$project" "$CADRE_LOGS"
  {
    echo "ts=$(date '+%F %T') project=$project repo=$repo"
    echo "user_branch=$(git -C "$repo" branch --show-current) user_head=$(git -C "$repo" rev-parse --short HEAD)"
    echo "user_dirty_files=$(git -C "$repo" status --porcelain | wc -l | tr -d ' ')"
    echo "base=$base base_sha=$(git -C "$repo" rev-parse --short "$start_ref")"
    echo "branch=$branch worktree=$wt"
  } >> "$CADRE_LOGS/worktrees.log"

  # Surface git's real error (append stderr to the log); stdout stays the
  # function's return channel.
  if ! git -C "$repo" worktree add -b "$branch" "$wt" "$start_ref" \
        >/dev/null 2>>"$CADRE_LOGS/worktrees.log"; then
    qe_die "git worktree add failed (see $CADRE_LOGS/worktrees.log)"
  fi
  # Install the guard hooks; if either config write fails, roll back so we
  # never leave an unguarded worktree/branch behind.
  if ! git -C "$repo" config extensions.worktreeConfig true \
     || ! git -C "$wt" config --worktree core.hooksPath "$CADRE_HOOKS"; then
    qe_die "failed to install guard hooks; worktree and branch retained: $wt ($branch)"
  fi
  echo "  -> created $branch at $wt" >> "$CADRE_LOGS/worktrees.log"

  # UNCOMMITTED WORK COMES ALONG TOO: changes are carried as a patch, untracked files (except gitignored) as copies;
  # the user's repository is READ-ONLY. Only if opened from the active branch (a patch does not apply on the fallback branch, so it is skipped).
  local dirty=0 f
  if [ -n "$src_sha" ]; then
    # C16: carry from the SNAPSHOT at acceptance time (not from the live repo); if the patch does not apply the job does NOT START (no half worktree is left).
    if [ "$src_carried" = "1" ]; then
      if [ "$src_patch" != "-" ]; then
        if ! git -C "$wt" apply --whitespace=nowarn --allow-empty "$src_patch" 2>>"$CADRE_LOGS/worktrees.log"; then
          echo "  HATA: kabul anindaki yama uygulanamadi -> worktree ve dal korundu" >> "$CADRE_LOGS/worktrees.log"
          qe_die "accepted snapshot patch does not apply; worktree and branch retained (see $CADRE_LOGS/worktrees.log)"
        fi
      fi
      if [ "$src_untracked" != "-" ] && [ -d "$src_untracked" ]; then
        ( cd "$src_untracked" && find . -type f -print0 ) | while IFS= read -r -d '' f; do
          mkdir -p "$wt/$(dirname "$f")" && cp -p "$src_untracked/$f" "$wt/$f"
        done
      fi
      echo "  carried_from_accepted_snapshot=1" >> "$CADRE_LOGS/worktrees.log"
    fi
  elif [ "$base" = "$cur" ]; then
    dirty="$(git -C "$repo" status --porcelain --untracked-files=all | wc -l | tr -d ' ')"
    if [ "$dirty" -gt 0 ]; then
      if ! git -C "$repo" diff --binary HEAD | git -C "$wt" apply --whitespace=nowarn --allow-empty \
            2>>"$CADRE_LOGS/worktrees.log"; then
        echo "  UYARI: commit'lenmemis degisiklik yamasi uygulanamadi" >> "$CADRE_LOGS/worktrees.log"
        printf 'cadre: UYARI commit\x27lenmemis degisiklikler tasinamadi (bkz worktrees.log)\n' >&2
      fi
      git -C "$repo" ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do
        mkdir -p "$wt/$(dirname "$f")" && cp -p "$repo/$f" "$wt/$f"
      done
      echo "  carried_uncommitted=$dirty" >> "$CADRE_LOGS/worktrees.log"
      printf 'cadre: aktif daldaki %s commit\x27lenmemis dosya worktree\x27ye tasindi\n' "$dirty" >&2
    fi
  fi

  if [ -n "${CADRE_RUN_ID:-}" ]; then
    python3 "${CADRE_ENGINE_ROOT:-$CADRE_HOME}/lib/kosu.py" ready-worktree "$CADRE_RUN_ID" "$wt" || return 1
  fi
  printf '%s\n' "$wt"
}

# Most recent existing worktree for a project, or empty.
qe_latest_worktree() {
  local project="$1"
  ls -1dt "$CADRE_WORKTREES/$project"/*/ 2>/dev/null | head -1 | sed 's:/*$::'
}

# Hard preflight before any editing session may start in a worktree.
# Exits non-zero (refuses) on any mismatch.
qe_git_common_dir() {
  local dir="$1" top common
  top="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR git -C "$dir" rev-parse --path-format=absolute --show-toplevel 2>/dev/null)" || return 1
  [ -n "$top" ] && [ "$(cd "$top" && pwd -P)" = "$(cd "$dir" && pwd -P)" ] || return 1
  common="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] && (cd "$common" && pwd -P)
}

qe_verify_ai_worktree() {
  local wt="$1" project="$2" branch common expected_common expected_repo hp h
  [ -d "$wt" ] || qe_die "worktree does not exist: $wt"
  case "$wt" in
    "$CADRE_WORKTREES/$project"/*) ;;
    *) qe_die "REFUSING: $wt is not under the central worktree area for $project" ;;
  esac
  branch="$(git -C "$wt" branch --show-current)"
  case "$branch" in
    cadre/*) ;;
    *) qe_die "REFUSING TO EDIT: branch '$branch' does not match cadre/*" ;;
  esac
  [ "$branch" != "$(qe_base_branch "$project")" ] \
    || qe_die "REFUSING TO EDIT: branch equals base branch"
  expected_repo="$(qe_repo_path "$project")"
  [ -n "$expected_repo" ] || qe_die "REFUSING TO EDIT: no repo-path configured for project '$project'"
  expected_common="$(qe_git_common_dir "$expected_repo")" \
    || qe_die "REFUSING TO EDIT: repo-path is not a git work tree root: $expected_repo"
  common="$(qe_git_common_dir "$wt")" \
    || qe_die "REFUSING TO EDIT: worktree is not a git work tree root: $wt"
  [ "$common" = "$expected_common" ] \
    || qe_die "REFUSING TO EDIT: worktree does not belong to $expected_repo (git-common-dir: $common, expected: $expected_common)"
  hp="$(git -C "$wt" config --worktree core.hooksPath 2>/dev/null)"
  [ "$hp" = "$CADRE_HOOKS" ] || qe_die "REFUSING TO EDIT: cadre guard hooks not installed in worktree"
  # A config pointer is worthless if git silently ignores a non-executable
  # hook file — verify the scripts actually exist and are executable.
  for h in pre-push pre-commit commit-msg; do
    [ -x "$CADRE_HOOKS/$h" ] || qe_die "REFUSING TO EDIT: guard hook '$h' missing or not executable"
  done
  return 0
}

# ---- qe_select: selection with the arrow keys (up/down + Enter) ----------------
# Why: the old version said "type 1 or 2" with `read -r`; arrow keys arrived as a raw
# escape sequence (\E[A). Without a TTY it falls back to numbered input.
qe_select() { # qe_select "Title" "option1" "option2" ... ; the chosen INDEX -> stdout (1-based)
  local title="$1"; shift
  local -a opts=("$@"); local n=${#opts[@]} cur=0 key rest sec ikinci
  # inside $(...) stdout is a PIPE; the interface draws to stderr, so the TTY test
  # must use stderr (fd 2). Looking at fd 1 always disabled the menu.
  if [ ! -t 0 ] || [ ! -t 2 ]; then
    { printf '\n%s\n' "$title"; local i=1
      for o in "${opts[@]}"; do printf '  %d) %s\n' "$i" "$o"; i=$((i+1)); done
      printf 'Seçim (Enter = 1): '; } >&2
    local a; read -r a || a=""
    case "$a" in ''|1) echo 1;; [0-9]) echo "$a";; *) echo 1;; esac; return
  fi
  printf '\n%s\n' "$title" >&2
  printf '  (↑/↓ ile seç, Enter ile onayla)\n\n' >&2
  local first=1
  while :; do
    [ "$first" = 1 ] && first=0 || printf '\033[%dA' "$n" >&2
    local i=0
    for o in "${opts[@]}"; do
      if [ "$i" = "$cur" ]; then printf '\033[2K\033[1;36m  ❯ %s\033[0m\n' "$o" >&2
      else                        printf '\033[2K    \033[2m%s\033[0m\n' "$o" >&2; fi
      i=$((i+1))
    done
    IFS= read -rsn1 key || { echo $((cur+1)); return; }
    case "$key" in
      $'\x1b') IFS= read -rsn2 -t 1 rest || rest=""   # bash 3.2: NO fractional -t
               case "$rest" in '[A') cur=$(( (cur-1+n)%n ));; '[B') cur=$(( (cur+1)%n ));; esac ;;
      '')      printf '\n' >&2; echo $((cur+1)); return ;;
      k|K|w|W) cur=$(( (cur-1+n)%n )) ;;
      j|J|s|S) cur=$(( (cur+1)%n )) ;;
      [1-9])   # the menu can be longer than 9 (cadre is one command). If one digit is not enough, wait BRIEFLY for a second digit;
               # if none comes, apply the single digit. So 1..9 select instantly, 10..N with two keys.
               sec="$key"
               if [ "$n" -ge 10 ]; then
                 IFS= read -rsn1 -t 1 ikinci || ikinci=""
                 case "$ikinci" in [0-9]) sec="$key$ikinci" ;; '') : ;; esac
               fi
               if [ "$sec" -ge 1 ] 2>/dev/null && [ "$sec" -le "$n" ]; then printf '\n' >&2; echo "$sec"; return; fi ;;
      q|Q)     printf '\n' >&2; echo 0; return ;;
    esac
  done
}

# ---- qe_read_task: MULTI-LINE task input (a single `read -r` leaked line 2 of a pasted prompt into the next question)
qe_read_task() { # stdout -> task text
  # User decision: EVERY CHOICE uses the arrow keys (no hidden typing rule).
  local task extra f how
  how="$(qe_select "Görevi nasıl yazacaksın?" \
        "Kısa — tek satır, buraya yazayım" \
        "Uzun — editör açılsın (çok satırlı, paragraflı)")"
  case "$how" in
    0) printf ''; return 0 ;;
    2)
      f="$(mktemp -t cadre-task).md"
      { printf '# Görevi buraya yaz. Bu # satırları yok sayılır.\n'
        printf '# Kaydet ve çık — nano: Ctrl-O, Enter, Ctrl-X · vim: :wq\n\n'; } > "$f"
      "${EDITOR:-${VISUAL:-nano}}" "$f" >&2 </dev/tty || true
      task="$(grep -v '^#' "$f" | sed -e :a -e '/^[[:space:]]*$/{$d;N;ba' -e '}')"
      rm -f "$f"
      ;;
    *)
      printf '\n> ' >&2
      IFS= read -r task || task=""
      # COLLECT the extra lines that come from a paste (so they do not leak into the next prompt).
      # bash 3.2 does not support a FRACTIONAL -t; an integer is required.
      while IFS= read -r -t 1 extra; do task="$task"$'\n'"$extra"; done
      ;;
  esac
  printf '%s' "$task"
}
