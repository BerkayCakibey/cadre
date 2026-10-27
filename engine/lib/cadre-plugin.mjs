import ProviderBudget from './butce-plugin.mjs';
import MonthlyBudget from './aylik-butce-plugin.mjs';
import { writeFileSync, renameSync, unlinkSync, lstatSync } from 'node:fs';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Explicit composition keeps the monthly guard outside the provider-specific
// guard: the monthly hold is created before provider admission and is released
// only when the inner gate proves that no network request was sent.
export default async function CadreBudget(context = {}) {
  const provider = await ProviderBudget();
  const monthly = await MonthlyBudget();
  const bounded = process.env.CADRE_PROJECT_TOOLS === '1';
  const directory = context.directory || process.cwd();
  const worktree = context.worktree || directory;
  const python = process.env.CADRE_PYTHON || '/usr/bin/python3';
  const oturum = fileURLToPath(new URL('./oturum.py', import.meta.url));
  // FIX-26 (N-ENV02): a session without CADRE_RUN_ID that runs inside a run sandbox is a nested executor, not a manual session.
  const nested = !process.env.CADRE_RUN_ID && spawnSync(python, [oturum, 'nested-check'],
    { encoding: 'utf8', timeout: 10000, env: { PATH: '/usr/bin:/bin', HOME: process.env.HOME, PYTHONDONTWRITEBYTECODE: '1' } }).status !== 0;
  if (process.env.CADRE_PROJECT_TOOLS === '1') {
    const policy = JSON.parse(process.env.CADRE_TOOL_POLICY);
    const owner = policy.owner;
    if (policy.process_scope && owner) {
      const directory = lstatSync(owner.directory);
      if (!directory.isDirectory() || directory.isSymbolicLink()
          || directory.dev !== owner.identity[0] || directory.ino !== owner.identity[1]) {
        throw new Error('CADRE_TOOL_DENIED: untrusted process scope owner');
      }
      const marker = join(owner.directory, 'tools-armed.json');
      const temporary = `${marker}.${randomUUID()}`;
      try {
        writeFileSync(temporary, JSON.stringify({ owner, pid: process.pid,
          profile_sha256: createHash('sha256').update(policy.profile).digest('hex') }), { flag: 'wx', mode: 0o400 });
        renameSync(temporary, marker);
      } finally {
        try { unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
    }
  }
  return {
    async 'tool.execute.before'(input, output) {
      if (nested) throw new Error('CADRE_TOOL_DENIED: nested executor inside a run sandbox');
      if (!bounded && !['todoread', 'todowrite', 'question', 'skill'].includes(input.tool)) {
        const result = spawnSync(python, [oturum, 'manual-check', worktree, directory, input.tool],
          { input: JSON.stringify(output.args || {}), encoding: 'utf8', timeout: 10000,
            env: { PATH: '/usr/bin:/bin', HOME: process.env.HOME, CADRE_HOME: process.env.CADRE_HOME,
              PYTHONDONTWRITEBYTECODE: '1' } });
        if (result.status !== 0) throw new Error('CADRE_TOOL_DENIED: manual scope requires an allowed source tool');
      }
      if (provider['tool.execute.before']) await provider['tool.execute.before'](input, output);
    },
    async config(config) {
      if (provider.config) await provider.config(config);

      // the local model was removed; if an old (live) config still defines lmstudio it is not probed.
      if (Array.isArray(config.enabled_providers)) {
        config.enabled_providers = config.enabled_providers.filter((name) => name !== 'lmstudio');
      }
      if (config.provider?.lmstudio) delete config.provider.lmstudio;

      if (monthly.config) await monthly.config(config);
      for (const [name, settings] of Object.entries(config.provider || {})) {
        const previous = settings.options?.fetch;
        if (!previous) continue;
        settings.options.fetch = async (url, options = {}) => {
          if (!process.env.CADRE_RUN_ID) {
            if (nested) throw new Error('CADRE_BUDGET_DENIED: nested executor inside a run sandbox');
            return previous(url, options);
          }
          let model = '';
          if (typeof options.body === 'string') {
            try { model = JSON.parse(options.body).model || ''; } catch {}
          }
          const checked = spawnSync(python,
            [fileURLToPath(new URL('./izin.py', import.meta.url)), 'model-check', process.env.CADRE_RUN_ID || '', name, model],
            { encoding: 'utf8', timeout: 10000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
          if (checked.status !== 0) throw new Error('CADRE_BUDGET_DENIED: operator model permission required');
          return previous(url, options);
        };
      }
    },
    async 'chat.params'(input, output) {
      if (provider['chat.params']) await provider['chat.params'](input, output);
      if (monthly['chat.params']) await monthly['chat.params'](input, output);
    },
    async 'chat.headers'(input, output) {
      if (provider['chat.headers']) await provider['chat.headers'](input, output);
      if (monthly['chat.headers']) await monthly['chat.headers'](input, output);
    },
  };
}
