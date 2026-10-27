# Install (macOS, manual)

Cadre is frozen and unsupported; these steps describe how it was run in October 2026.

1. Prerequisites, installed by you: macOS with `/usr/bin/sandbox-exec`, Git, Bash, Python 3.12+, Node 22+, OpenCode CLI 1.18.x (the budget plugin uses the V1 plugin API; the check accepts later 1.x releases with a warning and rejects older ones and 2.x). The engine runs `/opt/homebrew/bin/opencode` unless `CADRE_REAL_OPENCODE` names another binary. Optional: a separate Python environment with Textual and Rich for the panel (`CADRE_UI_PYTHON`).
2. Provider keys live in OpenCode's local configuration, never in this repository. OpenRouter was the paid provider in the final runs; `engine/lib/openrouter-politika.json` pins allowed endpoints and prices per model.
3. `python3 engine/lib/kurulum_kontrol.py` prints what is missing, using the same OpenCode binary rule as the engine. It installs and writes nothing; after step 5 the same check is `cadre kurulum-kontrol`.
4. Adapt `config/opencode.jsonc`; the plugin entry points at `~/.local/share/cadre-engineering/lib/cadre-plugin.mjs`.
5. `./sync.sh plan` shows what would change. `./sync.sh push` writes outside this repository: the engine into `CADRE_HOME` (default `~/.local/share/cadre-engineering`), a `cadre` launcher into `CADRE_BIN_DIR` (default `~/.local/bin`), and agents, skills, commands and `AGENTS.md` into `OPENCODE_CFG` (default `~/.config/opencode`); running guards are restarted. The launcher keeps the `CADRE_HOME` and `OPENCODE_CFG` used at install time unless they are set when it runs. An existing file it did not write before stops the push with `ownership-unknown` until you take it over with `./sync.sh adopt`. Set all three to a throwaway location for a first try.
6. `cadre init <repo>` registers a project. Put `PROJECT.md`, `POLICY.md` and the project's `qa-komut` (format, lint, type and test commands for the files you expect to change) under `projects/<name>/`.
7. Run one small permitted job and read its record under `runs/<run-id>/` before trusting anything.

Last tested with macOS 27 (arm64), Python 3.14.6, Node 26.5.0, OpenCode 1.18.31 and, for the panel, Textual 8.2.8 with Rich 15.0.0; the showcase CI uses Python 3.12 and Node 22. There is no lock file: the engine uses only the standard library, and the panel is optional.

Environment variables that matter: `CADRE_HOME`, `CADRE_BIN_DIR`, `OPENCODE_CFG`, `CADRE_ENGINE_ROOT`, `CADRE_ZINCIR_PLAN/IMPL/REVIEW/KALITE/BIRLESTIRME/KURTARICI` (model route per role, e.g. `openrouter/deepseek/deepseek-v4-pro`), `CADRE_KOSU_TAVAN_USD`, `CADRE_QA_TIMEOUT`.
