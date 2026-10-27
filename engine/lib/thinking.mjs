// 4c-4: the thinking level follows the endpoint's support. Values come only from the catalogue's reasoning_options; effort for an endpoint
// that supports levels, budget for one that supports only a budget, never both; an empty list sends no field.
import { readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const ORDER = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
const ROLE_LEVEL = { plan: 'high', 'cadre-plan': 'high', quality: 'high' };   // decision roles; others use the provider default
// 4c-10: the lane's model does the compaction; there, the lowest level the model supports ('none' -> nearest allowed)

let cache = { path: null, stamp: null, data: {} };
function catalog() {
  const path = process.env.CADRE_MODEL_CATALOG || join(homedir(), '.cache/opencode/models.json');
  try {
    const stamp = statSync(path).mtimeMs;
    if (cache.path !== path || cache.stamp !== stamp) cache = { path, stamp, data: JSON.parse(readFileSync(path, 'utf8')) };
  } catch { cache = { path, stamp: null, data: {} }; }
  return cache.data;
}

export function options(provider, model) {
  const record = catalog()?.[provider]?.models?.[model];
  return Array.isArray(record?.reasoning_options) ? record.reasoning_options : [];
}

function nearest(requested, allowed) {
  const ordered = [...allowed].sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
  return ordered.find((v) => ORDER.indexOf(v) >= ORDER.indexOf(requested)) ?? ordered.at(-1);
}

// Wire format: OpenRouter `reasoning.effort`, Responses API `reasoning.effort`, compatible chat `reasoning_effort`.
function field_list(body, provider, url) {
  if (/\/messages(\?|$)/.test(String(url))) {
    // Anthropic format (Go qwen): a probe (07.10) accepted output_config.effort; the mode is pinned to effort, budgeted thinking
    // is removed and max_tokens drops to the visible output cap (so a catalogue refresh cannot switch the mode to budget and inflate the cap).
    return {
      anthropic: true,
      read: () => body.output_config?.effort,
      write: (value) => { body.output_config = { ...(body.output_config ?? {}), effort: value }; },
      remove: () => { if (body.output_config) delete body.output_config.effort; },
      removeBudget: () => {
        const budget = body.thinking?.budget_tokens;
        if (Number.isSafeInteger(budget) && Number.isSafeInteger(body.max_tokens) && body.max_tokens > budget) body.max_tokens -= budget;
        delete body.thinking;
      },
    };
  }
  const object = provider === 'openrouter' || /\/responses(\?|$)/.test(String(url));
  return {
    read: () => (object ? body.reasoning?.effort : body.reasoning_effort),
    write: (value) => { if (object) body.reasoning = { ...(body.reasoning ?? {}), effort: value }; else body.reasoning_effort = value; },
    remove: () => { if (object && body.reasoning) delete body.reasoning.effort; else delete body.reasoning_effort; },
    removeBudget: () => {   // only the budget number is dropped; the on/off switch (thinking.type) is left to the provider
      if (body.reasoning) delete body.reasoning.max_tokens;
      if (body.thinking && typeof body.thinking === 'object') delete body.thinking.budget_tokens;
    },
  };
}

export function sentEffort(body, provider, url) {
  return field_list(body, provider, url).read();
}

// Returns: true if the body changed.
export function thinkingSet(body, { provider, model, role, agent, url }) {
  const field = field_list(body, provider, url);
  if (!field) return false;
  const opts = options(provider, model);
  const effort = opts.find((o) => o?.type === 'effort');
  const allowed = Array.isArray(effort?.values) ? effort.values.filter((v) => ORDER.includes(v)) : [];
  const once = JSON.stringify(body);
  const existing = field.read();
  const economic = role === 'impl' && !ROLE_LEVEL[agent] && ORDER.includes(process.env.CADRE_EFFORT) ? process.env.CADRE_EFFORT : null;
  let requested = agent === 'compaction' ? 'none'
    : String(model).includes('gpt-6.1-sol') ? 'high' : (ROLE_LEVEL[role] ?? ROLE_LEVEL[agent] ?? economic);
  if (!requested && field.anthropic && body.thinking?.budget_tokens !== undefined && existing === undefined) requested = 'medium';
  if (!allowed.length) {
    if (existing !== undefined) field.remove();   // no level is sent to an endpoint that does not support levels
  } else {
    const target = requested ? nearest(requested, allowed) : (existing !== undefined && !allowed.includes(existing) ? nearest(existing, allowed) : null);
    if (target) field.write(target);
    if (field.read() !== undefined) field.removeBudget();   // level and budget are never sent together
  }
  return JSON.stringify(body) !== once;
}
