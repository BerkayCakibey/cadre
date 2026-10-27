// 4c-4: the thinking level follows the endpoint's support. Values come only from the catalogue's reasoning_options; effort for an endpoint
// that supports levels, budget for one that supports only a budget, never both; an empty list sends no field.
import { readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const SIRA = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
const ROL_SEVIYE = { plan: 'high', 'cadre-plan': 'high', kalite: 'high' };   // decision roles; others use the provider default
// 4c-10: the lane's model does the compaction; there, the lowest level the model supports ('none' -> nearest allowed)

let onbellek = { yol: null, damga: null, veri: {} };
function katalog() {
  const yol = process.env.CADRE_MODEL_KATALOG || join(homedir(), '.cache/opencode/models.json');
  try {
    const damga = statSync(yol).mtimeMs;
    if (onbellek.yol !== yol || onbellek.damga !== damga) onbellek = { yol, damga, veri: JSON.parse(readFileSync(yol, 'utf8')) };
  } catch { onbellek = { yol, damga: null, veri: {} }; }
  return onbellek.veri;
}

export function secenekler(provider, model) {
  const kayit = katalog()?.[provider]?.models?.[model];
  return Array.isArray(kayit?.reasoning_options) ? kayit.reasoning_options : [];
}

function yakin(istenen, izinli) {
  const sirali = [...izinli].sort((a, b) => SIRA.indexOf(a) - SIRA.indexOf(b));
  return sirali.find((v) => SIRA.indexOf(v) >= SIRA.indexOf(istenen)) ?? sirali.at(-1);
}

// Wire format: OpenRouter `reasoning.effort`, Responses API `reasoning.effort`, compatible chat `reasoning_effort`.
function alanlar(body, provider, url) {
  if (/\/messages(\?|$)/.test(String(url))) {
    // Anthropic format (Go qwen): a probe (07.10) accepted output_config.effort; the mode is pinned to effort, budgeted thinking
    // is removed and max_tokens drops to the visible output cap (so a catalogue refresh cannot switch the mode to budget and inflate the cap).
    return {
      anthropic: true,
      oku: () => body.output_config?.effort,
      yaz: (deger) => { body.output_config = { ...(body.output_config ?? {}), effort: deger }; },
      sil: () => { if (body.output_config) delete body.output_config.effort; },
      butceSil: () => {
        const butce = body.thinking?.budget_tokens;
        if (Number.isSafeInteger(butce) && Number.isSafeInteger(body.max_tokens) && body.max_tokens > butce) body.max_tokens -= butce;
        delete body.thinking;
      },
    };
  }
  const nesne = provider === 'openrouter' || /\/responses(\?|$)/.test(String(url));
  return {
    oku: () => (nesne ? body.reasoning?.effort : body.reasoning_effort),
    yaz: (deger) => { if (nesne) body.reasoning = { ...(body.reasoning ?? {}), effort: deger }; else body.reasoning_effort = deger; },
    sil: () => { if (nesne && body.reasoning) delete body.reasoning.effort; else delete body.reasoning_effort; },
    butceSil: () => {   // only the budget number is dropped; the on/off switch (thinking.type) is left to the provider
      if (body.reasoning) delete body.reasoning.max_tokens;
      if (body.thinking && typeof body.thinking === 'object') delete body.thinking.budget_tokens;
    },
  };
}

export function gonderilenEfor(body, provider, url) {
  return alanlar(body, provider, url).oku();
}

// Returns: true if the body changed.
export function dusunmeAyarla(body, { provider, model, rol, ajan, url }) {
  const alan = alanlar(body, provider, url);
  if (!alan) return false;
  const opts = secenekler(provider, model);
  const effort = opts.find((o) => o?.type === 'effort');
  const izinli = Array.isArray(effort?.values) ? effort.values.filter((v) => SIRA.includes(v)) : [];
  const once = JSON.stringify(body);
  const mevcut = alan.oku();
  const ekonomik = rol === 'impl' && !ROL_SEVIYE[ajan] && SIRA.includes(process.env.CADRE_EFOR) ? process.env.CADRE_EFOR : null;
  let istenen = ajan === 'compaction' ? 'none'
    : String(model).includes('gpt-6.1-sol') ? 'high' : (ROL_SEVIYE[rol] ?? ROL_SEVIYE[ajan] ?? ekonomik);
  if (!istenen && alan.anthropic && body.thinking?.budget_tokens !== undefined && mevcut === undefined) istenen = 'medium';
  if (!izinli.length) {
    if (mevcut !== undefined) alan.sil();   // no level is sent to an endpoint that does not support levels
  } else {
    const hedef = istenen ? yakin(istenen, izinli) : (mevcut !== undefined && !izinli.includes(mevcut) ? yakin(mevcut, izinli) : null);
    if (hedef) alan.yaz(hedef);
    if (alan.oku() !== undefined) alan.butceSil();   // level and budget are never sent together
  }
  return JSON.stringify(body) !== once;
}
