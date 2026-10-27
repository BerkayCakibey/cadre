# Security

Cadre is an experimental, frozen project published as a showcase. It is not supported and receives no scheduled security updates.

What the engine does to protect the operator (as designed and tested locally on macOS):

- Every paid model call needs a typed proposal and two separate operator approvals; the proposal pins the source digest, the QA command hash, the models and a fixed money cap.
- Implementation runs in an isolated git worktree. In an engine run the OpenCode plugin rejects every tool except the shell (and its own to-do list) and wraps each shell command in a per-task `sandbox-exec` profile (`oturum.project_tool_policy`, `butce-plugin.mjs`): writes only to the planned files and a scratch directory (the QA repair round may write anywhere in the worktree), no network, no Mach lookups, reads restricted to configured roots (including system directories and interpreter locations), credential-shaped paths (`.ssh`, `.aws`, `auth.json`, `*.pem`, `.netrc`, signature files) denied, package installers blocked.
- `engine/bin/kisitli-kabuk.sh` is not a shell: it hashes the OpenCode config and `auth.json` before a run and restores them if the run changed or deleted them.
- Commit, push, merge, installation and publication are separate operator decisions; the engine never pushes on its own.
- Before each paid call, the engine checks its reservation against the approved cap using recorded price and usage bounds, then reconciles provider receipts in a local ledger. No typed-request run exceeded its cap in the recorded runs. The control depends on the configured providers and their reported prices and usage.

Known limits:

- macOS `sandbox-exec` is deprecated by Apple. The profile starts from `(allow default)` and denies what it knows about; system directories such as `/usr`, `/Library` and `/opt` stay readable.
- The readable area grows with the configured interpreters: for `CADRE_TEST_PYTEST_PYTHON` and `CADRE_UI_PYTHON` the directory two levels above the binary becomes readable. An interpreter at `~/bin/python` would open the whole home directory except the denied credential paths, which do not cover `~/.docker`, `~/.gnupg` or the keychains.
- Installers are blocked by binary name; the network deny is the real protection against downloads.
- `kisitli-kabuk.sh` keeps a backup copy of `auth.json` in `$TMPDIR/cadre-imza-<uid>` (mode 0700) during a run; after a crashed run it stays until a later run removes backups older than 24 hours, so delete that directory yourself if no run follows.
- Run records under `runs/` and `reports/` in the runtime home are private recovery data: full transcripts, patches and copies of untracked files, with only common token formats masked. The example under `examples/` was sanitized by hand; do not publish raw run directories.
- `./sync.sh pull` copies the live agents, skills and OpenCode config back into the repository and masks only `apiKey` values; review that diff before committing it.
- The sample `config/opencode.jsonc` is for manual sessions and only denies listed commands; plain `git push`, `curl` or `pip install` are not blocked there. The protections above apply to engine runs, not to manual sessions with that file.
- The OpenCode plugin targets the V1 API. Secrets are expected in OpenCode's local configuration, not in this repository.

The project is frozen. Issues are open, but there is no support, no bug bounty and no guaranteed response or fix; fork it if you want to change something. Do not put credentials or exploit details in a public issue; report a security problem privately through the repository's Security tab ("Report a vulnerability").
