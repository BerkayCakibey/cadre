// OpenCode provider fetch boundary. No SDK dependency and no sidecar server.
// Native LLM transport must be disabled by the launcher: it bypasses SDK fetch.
import { execFileSync, execFile } from 'node:child_process';
import { mkdirSync, writeFileSync, unlinkSync, readFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { roleOutput, requestedOutput, LIMITS } from './limits.mjs';   // 4c-4: output caps from one file (plan 64k, others 32k)
import { RequestContext } from './request-context.mjs';
import { thinkingSet, sentEffort } from './thinking.mjs';
import * as carry from './thinking-carry.mjs';

const supported = new Set(['opencode-go', 'opencode', 'openrouter']);  // 0c: 'opencode' = zen; 4c-11: no free provider path
// B124: OBSERVATION MODE. In manual `opencode` use the gate does NOT BLOCK, it only observes — spending is written to the ledger,
// the call is not denied. In a run environment (CADRE_RUN_ID set) the mode is IGNORED: automated work is always subject to the cap.
const observationMode = () => process.env.CADRE_BUDGET_OBSERVATION === '1' && !process.env.CADRE_RUN_ID;
const budgetRejection = (error) => String(error?.message ?? '').startsWith('CADRE_BUDGET_DENIED');
// G-B5 (B154): the denial receipt CARRIES the provider; if not, the engine falls back to the cautious default (all cloud closed).
const deny = (provider = null, reason = 'plugin refusal') => {
  try {
    execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/budget_gate.py')], {
      input: JSON.stringify({ action: 'deny', provider, reason }), timeout: 5000, stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch { /* Refuse even if recording the refusal is impossible. */ }
  throw new Error('CADRE_BUDGET_DENIED: no verified request allowance');
};
const number = (value) => {
  // B169: an invalid number is NOT DENIED here (provider unknown); returns NaN, the caller denies with the provider.
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return Number.NaN;
  return value;
};

function ledger(request) {
  try {
    return JSON.parse(execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/budget_gate.py')], {
      input: JSON.stringify(request), encoding: 'utf8', timeout: 45000, maxBuffer: 65536,
      stdio: ['pipe', 'pipe', 'pipe'],
    }));
  } catch { return deny(request?.provider ?? null, 'ledger refused or unavailable'); }   // B169: the denial is recorded with the provider
}

function enabledOutput(model, agent) {
  const modelLimit = number(model?.limit?.output);
  if (!Number.isSafeInteger(modelLimit) || modelLimit <= 0) return Number.NaN;
  return Math.min(roleOutput(process.env.CADRE_ROLE ?? '', agent), modelLimit);
}

// Item 0b: the bucket unit is the MODEL — a zero-priced model counts REQUESTS from the '<provider>:free' bucket.
function zeroCost(model) {
  const cost = model?.cost;
  if (!cost || typeof cost.input !== 'number' || typeof cost.output !== 'number') return false;
  return cost.input === 0 && cost.output === 0;
}

// OR-PIN: the reservation uses the MOST EXPENSIVE input/output rate of the pinned providers (the catalogue carries the cheapest);
// the price is written to chain/openrouter-price.json by provider_budget.py `or_price()`. If the file is missing/incomplete the reservation
// is DENIED (fail-closed): an unknown expensive route never produces a silent overrun. 4c-3/7: an endpoint that does not support the body's token parameter or
// cannot carry the requested output is dropped; order = this request's tokens x live price, endpoints with a low uptime ratio go last.
function orEndpointAllowed(rule, m, endpoint) {
  if (Array.isArray(rule)) return rule.includes(endpoint);
  if (!rule || typeof rule !== 'object') return false;
  const list = rule.tiers ?? rule.providers;
  if (Array.isArray(list)) return list.includes(endpoint);
  return rule.closed === true || m?.tiers?.[endpoint]?.quantization === (rule.quantization ?? 'fp8');
}

function orMostExpensive(model, body = null, session = null) {
  const entry_name = String(model?.api?.id ?? model?.id ?? '').replace(/^openrouter\//, '');
  if (!entry_name) return null;
  try {
    const d = JSON.parse(readFileSync(join(process.env.CADRE_HOME ?? '', 'chain', 'openrouter-price.json'), 'utf8'));
    const m = (d?.models ?? {})[entry_name];
    const now_ts = Date.now() / 1000, age = now_ts - d.ts, priceAge = now_ts - (m?.price_ts ?? d.ts);
    const limit = LIMITS.openrouter;
    if (d.version !== 2 || !Number.isFinite(age) || age < 0 || age >= limit.price_fresh_sec
        || !Number.isFinite(priceAge) || priceAge < 0 || priceAge >= limit.price_age_sec) return null;
    const rule = JSON.parse(readFileSync(new URL('./openrouter-policy.json', import.meta.url), 'utf8'))[entry_name];
    const object = !!rule && !Array.isArray(rule) && typeof rule === 'object';
    if (!rule || !Array.isArray(m?.providers) || !m.providers.length ||
        m.fp8_cache !== true || m.providers.some((endpoint) => !orEndpointAllowed(rule, m, endpoint))) return null;
    let providers = m.providers;
    let input = number(m?.input), output = number(m?.output);
    const choice = { reason: [], eliminated: [] };
    let inputTok = null;
    if (body) {
      let status = {};
      try {
        status = JSON.parse(readFileSync(process.env.CADRE_ROUTER_STATUS ?? join(process.env.CADRE_HOME ?? '', 'chain', 'provider-status.json'), 'utf8'));
      } catch (error) { if (error.code !== 'ENOENT') return null; }
      const cooling = status?.[`openrouter/${entry_name}`]?.endpoints ?? {};
      const open = providers.filter((p) => Number(cooling[p]?.cooldown_until ?? 0) <= now_ts);
      // If no endpoint is open, keep the selection: the native reservation gate gives the recorded wait reason, no network is opened.
      if (open.length) {
        for (const p of providers) if (!open.includes(p)) choice.eliminated.push(`${p}: endpoint cooldown`);
        providers = open;
      }
    }
    if (body && m.tiers) {
      const param = body.max_completion_tokens !== undefined ? 'max_completion_tokens' : 'max_tokens';
      let requested = Number(body.max_completion_tokens ?? body.max_tokens ?? 0);
      const matching = providers.filter((p) => !Array.isArray(m.tiers[p]?.params) || m.tiers[p].params.includes(param));
      const carries = (p, n) => !(Number.isSafeInteger(m.tiers[p]?.max_output) && n > m.tiers[p].max_output);
      providers = matching.filter((p) => carries(p, requested));
      for (const p of m.providers) {
        if (choice.eliminated.includes(`${p}: endpoint cooldown`)) continue;
        if (!matching.includes(p)) choice.eliminated.push(`${p}: ${param} is unsupported`);
        else if (!providers.includes(p)) choice.eliminated.push(`${p}: output limit ${m.tiers[p]?.max_output}`);
      }
      const caps = matching.map((p) => m.tiers[p]?.max_output).filter(Number.isSafeInteger);
      if (!providers.length && caps.length) {
        // 4c QA2: if 2× the request fits no endpoint after truncation, no endpoint is dropped; the request is clipped to the largest endpoint cap
        requested = Math.max(...caps);
        body[param] = requested;
        providers = matching.filter((p) => carries(p, requested));
        choice.eliminated = choice.eliminated.filter((line) => !providers.some((p) => line.startsWith(`${p}: output`)));
      }
      if (!providers.length) return null;
      inputTok = JSON.stringify(body).length + 4096;   // same rough estimate as requestQuote
      const expense = (p) => number(m.tiers[p]?.input ?? m.input) * inputTok + number(m.tiers[p]?.output ?? m.output) * requested;
      const low = (p) => ((m.tiers[p]?.uptime ?? 100) < limit.low_uptime ? 1 : 0);
      providers = providers.map((p, i) => [p, i])
        .sort((a, b) => low(a[0]) - low(b[0]) || expense(a[0]) - expense(b[0]) || a[1] - b[1]).map(([p]) => p);
      if (session) {
        const hint = hintRead();
        const last = String(process.env.CADRE_DEADLINE_TS ?? '').trim();
        const outcome = sessionOrder({ m, providers, input: inputTok, requested, now_ts, proceed: session.proceed,
          record: hint.sessions[JSON.stringify([session.session, entry_name])] ?? null,
          endpoints: Object.fromEntries(providers.map((p) => [p, hint.endpoints[JSON.stringify([entry_name, p])]]).filter(([, v]) => v)),
          remainingSec: last && Number.isFinite(Number(last)) ? Number(last) - now_ts : Number.NaN,
          stickySec: Number(limit.session_sticky_sec ?? 0), errorSec: Number(limit.endpoint_error_sec ?? 0), lowUptime: limit.low_uptime });
        providers = outcome.providers;
        choice.reason.push(...outcome.reason);
        if (outcome.session) choice.session = outcome.session;
      }
      input = Math.max(...providers.map((p) => number(m.tiers[p]?.input ?? m.input)));
      output = Math.max(...providers.map((p) => number(m.tiers[p]?.output ?? m.output)));
    }
    if (!Number.isFinite(input) || !Number.isFinite(output) || input <= 0 || output <= 0) return null;
    const names = {};
    for (const p of providers) {
      const visible = m.tiers?.[p]?.entry_name;
      if (typeof visible === 'string' && visible) names[visible] = visible in names ? null : p;
    }
    return { input: input, output: output, providers, labeled: object && Array.isArray(rule.tiers),
             closed: object ? rule.closed === true : entry_name.startsWith('openai/'), maxPrice: object ? (rule.max_price ?? null) : null,
             choice, inputTok, names, priceTs: Number(m?.price_ts ?? d.ts) };
  } catch { return null; }
}

export function sessionOrder({ m, providers, input, requested, record, endpoints, remainingSec, now_ts, stickySec, errorSec, proceed = null, lowUptime = null }) {
  const reason = [];
  if (!(stickySec > 0) && !(errorSec > 0)) return { providers: [...providers], reason };
  const ratio = (p, field) => number(m.tiers?.[p]?.[field] ?? m[field]);
  const uptime = (p) => m.tiers?.[p]?.uptime ?? 100;
  const health = (p) => (Number.isFinite(lowUptime) && uptime(p) < lowUptime ? 1 : 0);
  const reading = (p) => {
    const r = m.tiers?.[p]?.cache;
    return typeof r === 'number' && Number.isFinite(r) && r >= 0 && r < ratio(p, 'input') ? r : ratio(p, 'input');
  };
  const group = (p) => {
    const u = endpoints?.[p];
    if (errorSec > 0 && u?.last === 'error' && now_ts - Number(u.last_ts) < errorSec) return 2;
    if (Number.isFinite(remainingSec) && typeof u?.speed_tps === 'number' && u.speed_tps > 0 && requested / u.speed_tps > remainingSec) return 1;
    return 0;
  };
  const groups = new Map(providers.map((p) => [p, group(p)]));
  for (const [p, g] of groups) if (g) reason.push(`${p}: ${g === 2 ? 'last outcome error' : 'cannot finish within the remaining time at the measured speed'}`);
  let order = providers.map((p, i) => [p, i]).sort((a, b) => groups.get(a[0]) - groups.get(b[0]) || a[1] - b[1]).map(([p]) => p);
  const age = now_ts - Number(record?.ts);
  const fresh = stickySec > 0 && record && age >= 0 && age <= stickySec;
  const sessionEndpoint = fresh ? (record.endpoint ?? record.order) : null;
  let session;
  if (fresh && record.success === false) {
    reason.push('session endpoint failed on the last request: no stickiness');
  } else if (sessionEndpoint && order.includes(sessionEndpoint)) {
    const sameCategory = (p) => p !== sessionEndpoint && groups.get(p) === 0 && health(p) === health(sessionEndpoint);
    const healthy = order.find((p) => p !== sessionEndpoint && groups.get(p) === 0 && health(p) < health(sessionEndpoint));
    const alternative = order.find(sameCategory);
    if (groups.get(sessionEndpoint) !== 0) {
      reason.push(`session endpoint ${sessionEndpoint} not retained: ${groups.get(sessionEndpoint) === 2 ? 'error' : 'does_not_fit'}`);
    } else if (healthy) {
      reason.push(`session endpoint ${sessionEndpoint} not retained: uptime ${uptime(sessionEndpoint)} < ${lowUptime} (${healthy} healthy)`);
    } else if (!alternative) {
      order = [sessionEndpoint, ...order.filter((p) => p !== sessionEndpoint)];
      reason.push(`session endpoint ${sessionEndpoint} preserved: no suitable alternative`);
    } else {
      const realEndpoint = typeof record.endpoint === 'string' && record.endpoint === sessionEndpoint;
      const previous = proceed === true && realEndpoint && Number.isSafeInteger(record.character) && record.character > 0 ? record.character : 0;
      if (proceed === false) reason.push('prefix broken: no cache benefit');
      else if (proceed !== true) reason.push('prefix unverified: no cache benefit');
      else if (!realEndpoint) reason.push('actual endpoint unknown: no cache benefit');
      const read = Math.min(input, previous);
      session = { endpoint: sessionEndpoint, endpoint_source: realEndpoint ? 'response' : 'first_preference', prefix: proceed === true || proceed === false ? proceed : null,
                 cache_credit: read };
      const staying_cost = (input - read) * ratio(sessionEndpoint, 'input') + read * reading(sessionEndpoint) + requested * ratio(sessionEndpoint, 'output');
      const transition = input * ratio(alternative, 'input') + requested * ratio(alternative, 'output');
      if (staying_cost <= transition) {
        order = [sessionEndpoint, ...order.filter((p) => p !== sessionEndpoint)];
        reason.push(`session endpoint ${sessionEndpoint} preserved: staying_cost ${Math.round(staying_cost)} <= switching cost ${Math.round(transition)} (${alternative})`);
      } else {
        reason.push(`session endpoint ${sessionEndpoint} left: switching cost ${Math.round(transition)} (${alternative}) < staying_cost ${Math.round(staying_cost)}`);
      }
    }
  }
  return { providers: order, reason, ...(session ? { session } : {}) };
}

const hintPath = () => join(process.env.CADRE_HOME ?? '', 'chain', 'or-session-endpoints.json');

function hintRead() {
  try {
    const d = JSON.parse(readFileSync(hintPath(), 'utf8'));
    if (d?.version === 1 && d.sessions && typeof d.sessions === 'object' && d.endpoints && typeof d.endpoints === 'object') return d;
  } catch {}
  return { version: 1, sessions: {}, endpoints: {} };
}

function hintWrite(change) {
  try {
    const d = hintRead();
    change(d);
    const now_ts = Date.now() / 1000;
    const fresh = (records, cap) => Object.fromEntries(Object.entries(records)
      .filter(([, v]) => typeof v?.ts === 'number' && now_ts - v.ts < 86400)
      .sort((a, b) => b[1].ts - a[1].ts).slice(0, cap));
    d.sessions = fresh(d.sessions, 256);
    d.endpoints = fresh(d.endpoints, 128);
    mkdirSync(join(process.env.CADRE_HOME ?? '', 'chain'), { recursive: true });
    const temporary = `${hintPath()}.${process.pid}.${randomUUID().slice(0, 8)}`;
    writeFileSync(temporary, JSON.stringify(d));
    renameSync(temporary, hintPath());
  } catch {}
}

function sessionOutcome(bag, { success, endpointName = null, output = null, durationMs = null }) {
  if (!bag?.session) return;
  const label = (endpointName && bag.names[endpointName]) || bag.singleEndpoint || null;
  hintWrite((d) => {
    const now_ts = Date.now() / 1000;
    d.sessions[JSON.stringify([bag.session, bag.entry_name])] = { order: bag.order, endpoint: label, success, character: bag.character, ts: now_ts };
    if (!label) return;
    const key = JSON.stringify([bag.entry_name, label]);
    const endpoint = { success: 0, error: 0, ...(d.endpoints[key] ?? {}) };
    endpoint[success ? 'success' : 'error'] += 1;
    Object.assign(endpoint, { last: success ? 'success' : 'error', last_ts: now_ts, ts: now_ts });
    if (success && Number.isSafeInteger(output) && output >= 32 && durationMs >= 500) endpoint.speed_tps = Math.round(output / (durationMs / 1000) * 100) / 100;
    d.endpoints[key] = endpoint;
  });
}

const PREFIXES = new Map();
const summarize = (value) => createHash('sha256').update(JSON.stringify(value ?? null)).digest('hex');

function prefixMeasure(session, body) {
  const list = [body?.messages, body?.input, body?.contents].find(Array.isArray) ?? [];
  const system = list.filter((m) => m?.role === 'system' || m?.role === 'developer');
  const constant = summarize([body?.system ?? body?.instructions ?? body?.systemInstruction ?? null, body?.tools ?? null, system]).slice(0, 16);
  const all_items = summarize(list);
  const previous = session ? PREFIXES.get(session) : undefined;
  const proceed = previous ? previous.constant === constant && previous.n <= list.length && summarize(list.slice(0, previous.n)) === previous.all_items : null;
  if (session) {
    PREFIXES.delete(session);
    PREFIXES.set(session, { constant, n: list.length, all_items });
    while (PREFIXES.size > 512) PREFIXES.delete(PREFIXES.keys().next().value);
  }
  return { constant, proceed, message: list.length };
}

function effort(body, provider, url) {
  const level = text(sentEffort(body, provider, url), 16);
  const policy = text(process.env.CADRE_EFFORT_POLICY, 32);
  return level || policy ? { effort: { level: level ?? null, policy: policy ?? null } } : {};
}

const wholeNumber = (v) => (Number.isSafeInteger(v) && v >= 0 ? v : undefined);
const amount = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : undefined);
const text = (v, n) => (typeof v === 'string' && v.length > 0 && v.length <= n && /^[A-Za-z0-9 ._:/()+@-]+$/.test(v) ? v : undefined);

function usageSummary(u, format) {
  if (!u || typeof u !== 'object') return {};
  const reading = wholeNumber(u.prompt_tokens_details?.cached_tokens ?? u.input_tokens_details?.cached_tokens
                        ?? u.cache_read_input_tokens ?? u.cachedContentTokenCount);
  const write = wholeNumber(u.prompt_tokens_details?.cache_write_tokens ?? u.cache_creation_input_tokens
                        ?? u.prompt_tokens_details?.cache_creation_tokens);
  let input = wholeNumber(u.prompt_tokens ?? u.promptTokenCount ?? u.input_tokens);
  if (format === 'anthropic' && input !== undefined) input += (reading ?? 0) + (write ?? 0);
  const thinking = wholeNumber(u.completion_tokens_details?.reasoning_tokens ?? u.output_tokens_details?.reasoning_tokens ?? u.thoughtsTokenCount);
  let output = wholeNumber(u.completion_tokens ?? u.output_tokens ?? u.candidatesTokenCount);
  if (format === 'usage-metadata' && output !== undefined && thinking !== undefined) output += thinking;
  return { input, output, thinking, cache_reading: reading, cache_write: write, fee_usd: amount(u.cost),
           cache_discount_usd: amount(u.cache_discount), parent_fee_usd: amount(u.cost_details?.upstream_inference_cost),
           byok: typeof u.is_byok === 'boolean' ? u.is_byok : undefined };
}

function quote(model, maximum) {
  const provider_key = model?.providerID ?? null;
  const input = number(model.limit?.input ?? model.limit?.context);
  const output = Math.min(number(model.limit?.output), number(maximum));
  if (!Number.isSafeInteger(input) || !Number.isSafeInteger(output) || input <= 0 || output <= 0) return deny(provider_key, 'pricing unknown');
  // OR-PIN: the OpenRouter reservation uses the most expensive pinned provider rate; no cache discount is assumed
  // (Relace charged cached input at FULL price). The receipt closes with the real usage.cost in the response.
  if (provider_key === 'openrouter') {
    const mostExpensive = orMostExpensive(model);
    if (!mostExpensive) return deny(provider_key, 'openrouter fail-closed pricing unavailable');
    return { input, output, inRate: mostExpensive.input, freshRate: mostExpensive.input, readRate: mostExpensive.input,
             writeRate: mostExpensive.input, outRate: mostExpensive.output,
             maximum: Math.ceil(input * mostExpensive.input + output * mostExpensive.output) };
  }
  // Account for more expensive context tiers and cache writes too. The catalogue
  // is a price estimate, not a provider invoice; unknown prices deny paid work.
  const costs = [model.cost, model.cost?.context_over_200k].filter(Boolean);
  const freshRate = Math.max(...costs.map(c => number(c.input)));
  const writeRate = Math.max(...costs.map(c => number(c.cache?.write ?? c.cache_write ?? 0)));
  // B230: if the catalogue has no cache-read price the discount is UNKNOWN, not zero; an unknown discount is the input price.
  const readCosts = costs.map(c => c.cache?.read ?? c.cache_read).filter(v => typeof v === 'number');
  const readRate = readCosts.length ? Math.max(...readCosts.map(number)) : freshRate;
  // Item 6: the RESERVATION uses the most expensive input rate (safety), the RECEIPT closes with the real rates.
  // The old code also closed the receipt at the most expensive rate: cache reads cost 1/5 to 1/100 of the input price,
  // so at a 90% hit rate the cost was inflated 8.5 TIMES — the ledger recorded the cap, not reality.
  const inRate = Math.max(freshRate, writeRate, readRate);
  const outRate = Math.max(...costs.map(c => number(c.output)));
  if (!Number.isFinite(inRate) || !Number.isFinite(outRate) || inRate <= 0 || outRate <= 0) return deny(provider_key, 'pricing unknown');
  return { input, output, inRate, freshRate, readRate, writeRate, outRate,
           maximum: Math.ceil(input * inRate + output * outRate) };
}

  // The reservation is priced by the size of the REQUEST, not by the model's full context (B36).
function requestQuote(pricing, rawBody, output = pricing.output) {
  const chars = number(rawBody.length);
  const inputTokens = Math.min(pricing.input, chars + 4096);
  // With time-dependent pricing the reservation ALWAYS assumes peak (2x): off-peak, the excess comes back with the receipt,
  // at peak the budget is not exceeded. If an error must be made in an unknown direction, it is made in the expensive direction.
  // Output is the request's total cap (Anthropic thinking budget included): the worst case is priced.
  const moment = pricing.timed ? 2 : 1;
  return Math.ceil(moment * (inputTokens * pricing.inRate + Math.max(pricing.output, output) * pricing.outRate));
}

// 4c-13: Go charges 2× points for DeepSeek models at peak hours (01-04, 06-10 UTC, Mon-Fri) (opencode.ai/docs/go); the catalogue price
// is off-peak. The reservation always uses the peak price (fail-closed), the receipt closes with the multiplier at that time.
const timed = (provider, model) => provider === 'opencode-go' && String(model ?? '').startsWith('deepseek');
function momentFactor() {
  const now = new Date();
  const day = now.getUTCDay();
  if (day === 0 || day === 6) return 1;
  const hour = now.getUTCHours();
  return ((hour >= 1 && hour < 4) || (hour >= 6 && hour < 10)) ? 2 : 1;
}

const integer = (value, fallback = 0) => (Number.isSafeInteger(value) && value >= 0 ? value : fallback);

function usageCost(value, pricing, provider = null) {
  // OpenAI Responses API: usage is in `response.usage` of the `response.completed` event, named input/output_tokens.
  const usage = value.usage ?? value.usageMetadata ?? value.response?.usage;
  if (!usage) return null;
  // OR-PIN: an OpenRouter response with usage:{include:true} carries the REAL cost in `usage.cost` (USD). This
  // replaces our estimate at the most expensive provider price — the ledger records reality and the cap triggers correctly.
  if (provider === 'openrouter') {
    return typeof usage.cost === 'number' && Number.isFinite(usage.cost) && usage.cost >= 0
      && Number.isSafeInteger(Math.ceil(usage.cost * 1e6)) ? Math.ceil(usage.cost * 1e6) : null;
  }
  if (value.type === 'message') return anthropicCost(usage, pricing); // G-B2/B319: cache input is additive.
  const input = number(usage.prompt_tokens ?? usage.promptTokenCount ?? usage.input_tokens);
  const output = number(usage.completion_tokens ?? usage.candidatesTokenCount ?? usage.output_tokens);
  // Google reports reasoning separately; other supported compatible endpoints
  // include it in completion_tokens. Never interpret missing usage as zero.
  const reasoning = usage.completion_tokens === undefined ? number(usage.thoughtsTokenCount ?? 0) : 0;
  if (![input, output, reasoning].every(Number.isSafeInteger)) return deny(provider, 'usage not integer');
  // Item 6: input READ from cache and input WRITTEN to cache are priced separately. Providers report this under three
  // different names; all are counted INSIDE prompt_tokens, so they are subtracted from fresh input.
  const read = integer(usage.prompt_tokens_details?.cached_tokens ?? usage.input_tokens_details?.cached_tokens
                         ?? usage.cache_read_input_tokens ?? usage.cachedContentTokenCount);
  const written = integer(usage.cache_creation_input_tokens ?? usage.prompt_tokens_details?.cache_creation_tokens);
  const fresh = Math.max(0, input - Math.min(input, read + written));
  const readNumber = Math.min(input, read);
  const writtenNumber = Math.min(Math.max(0, input - readNumber), written);
  const freshRate = pricing.freshRate ?? pricing.inRate;
  const readRate = pricing.readRate ?? freshRate;
  const writeRate = pricing.writeRate || freshRate;
  const multiplier = pricing.timed ? momentFactor() : 1;
  return Math.ceil(multiplier * (fresh * freshRate + readNumber * readRate + writtenNumber * writeRate
                             + (output + reasoning) * pricing.outRate));
}

// B310: in the Anthropic Messages stream (Go qwen3.8-flash, @ai-sdk/anthropic) input_tokens does NOT INCLUDE the cache counts;
// usage is split across message_start + message_delta. A missing or invalid number returns null (no receipt, the reservation stays).
function anthropicCost(u, pricing) {
  const fresh = u.input_tokens, read = u.cache_read_input_tokens ?? 0, written = u.cache_creation_input_tokens ?? 0, output = u.output_tokens;
  if (![fresh, read, written, output].every(v => Number.isSafeInteger(v) && v >= 0)) return null;
  const freshRate = pricing.freshRate ?? pricing.inRate;
  const multiplier = pricing.timed ? momentFactor() : 1;
  return Math.ceil(multiplier * (fresh * freshRate + read * (pricing.readRate ?? freshRate)
                             + written * (pricing.writeRate || freshRate) + output * pricing.outRate));
}

// Receipt of the free bucket: what is measured is the REQUEST COUNT, not dollars, so 1 is written whatever the outcome.
function countFreeRequest(reservation, receivedID) {
  try {
    ledger({ action: 'settle', id: reservation.id, actual: 1,
      receipt: `${reservation.id}:${receivedID || 'free-request'}` });
  } catch { /* if the count cannot be written the reservation stays; no money risk */ }
}

// LIVE1/4c-4: the open-request marker carries the time and type of the last chunk. Progress = a token or usage chunk; an SSE comment
// (OR ': OPENROUTER PROCESSING') only shows the connection is open. The guard (liveness.py) reads progress; no fixed timeout.
const EMPTY_PART = /"type"\s*:\s*"ping"|"delta"\s*:\s*\{\s*(?:"role"\s*:\s*"assistant"\s*,?\s*)?(?:"content"\s*:\s*(?:""|null)\s*)?\}/;

function partKind(text) {
  let round = '';
  for (const line of text.split('\n')) {
    const data = line.startsWith('data:') ? line.slice(5).trim() : '';
    if (data && data !== '[DONE]') {
      if (/"usage"\s*:\s*\{/.test(data)) return 'usage';
      if (!EMPTY_PART.test(data)) round = 'token';
      else if (!round) round = 'comment';
    } else if (line.startsWith(':') && !round) round = 'comment';
  }
  return round;
}

async function liveFetch(fetcher, url, options, local = false) {
  const abort = new AbortController();
  const relay = () => abort.abort(options.signal.reason);
  if (options.signal?.aborted) relay();
  else options.signal?.addEventListener('abort', relay, { once: true });
  let marker, mark, lastWrite = 0;
  const decoder = new TextDecoder();
  const write = (force = false) => {
    if (!marker || (!force && Date.now() - lastWrite < 1000)) return;   // written at most once per second
    lastWrite = Date.now();
    try { writeFileSync(marker, JSON.stringify(mark)); } catch {}
  };
  if (process.env.CADRE_REQUEST_DIR) {
    mkdirSync(process.env.CADRE_REQUEST_DIR, { recursive: true });
    marker = join(process.env.CADRE_REQUEST_DIR, `${randomUUID()}.json`);
    mark = { pid: process.pid, started: Date.now(), local, last_chunk_at: null, progress_at: null, round: '' };
    write(true);
  }
  const cleanup = () => {
    options.signal?.removeEventListener('abort', relay);
    if (marker) { try { unlinkSync(marker); } catch {} marker = undefined; }
  };
  let response;
  try { response = await fetcher(url, { ...options, signal: abort.signal }); }
  catch (error) { cleanup(); throw error; }
  if (!response.body) { cleanup(); return response; }
  const sse = (response.headers.get('content-type') ?? '').includes('text/event-stream');
  const reader = response.body.getReader();
  const body = new ReadableStream({
    async pull(controller) {
      try {
        const chunk = await reader.read();
        if (chunk.done) { cleanup(); controller.close(); return; }
        if (mark) {
          const round = sse ? partKind(decoder.decode(chunk.value, { stream: true })) : 'token';
          const progress = round === 'token' || round === 'usage';
          const first = progress && mark.progress_at === null;
          mark.last_chunk_at = Date.now();
          if (round) mark.round = round;
          if (progress) mark.progress_at = mark.last_chunk_at;
          write(first);
        }
        controller.enqueue(chunk.value);
      } catch (error) {
        cleanup(); controller.error(error);
        void reader.cancel(error).catch(() => {});
      }
    },
    cancel(reason) { cleanup(); abort.abort(reason); return reader.cancel(reason); },
  });
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
}

function productionSave(id, generation) {
  try {
    execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/budget_gate.py')], {
      input: JSON.stringify({ action: 'gen', id, generation }), timeout: 10000, stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch {}
}

function providerError(reservation, error, code) {
  if (!error || typeof error !== 'object') return;
  try {
    execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/budget_gate.py')], {
      input: JSON.stringify({ action: 'provider-error', id: reservation.id, code: Number.isInteger(error.code) ? error.code : code,
        round: text(error.metadata?.error_type, 64) ?? '', message: text(error.message, 160) ?? '' }),
      timeout: 10000, stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch {}
}

async function receiptStream(response, reservation, pricing, isFree, provider = null, session = null, model = '', extra = {}) {
  // G-B2/P5: a free request that was sent counts even if the body fails later.
  if (isFree) countFreeRequest(reservation, '');
  if (!isFree && response.status === 429) {
    // 4c-4: 429 says the request was not processed; the SDK retries with the same header, no double obligation is left (the receipt stays recorded)
    try { ledger({ action: 'settle', id: reservation.id, actual: 0, receipt: `${reservation.id}:http-429` }); } catch {}
    sessionOutcome(extra.orContext, { success: false });
    return response;
  }
  if (!response.ok || !response.body) {
    if (!response.ok) sessionOutcome(extra.orContext, { success: false });
    if (!isFree && response.status >= 500) {
      let body = null;
      try { body = JSON.parse(await response.clone().text()); } catch {}
      providerError(reservation, body?.error ?? { code: response.status }, response.status);
    }
    return response;                                    // Other denials may carry an unknown amount: the reservation stays.
  }
  const sse = (response.headers.get('content-type') ?? '').includes('text/event-stream');
  const decoder = new TextDecoder();
  let buffer = '', used = null, finished = false, invalid = false, receivedID = '', anth = null, reasoningComplete = false;
  let productionWritten = false, errorWritten = false;
  let lastUsage = null, discount, endpointName, modelResponse, end;
  const summaries = provider === 'openrouter' ? carry.accumulation() : null;
  const item = (raw) => {
    if (raw === '[DONE]') { finished = true; return; }
    try {
      const value = JSON.parse(raw);
      if (summaries) carry.part(summaries, value);
      if (typeof value?.provider === 'string') endpointName = value.provider;
      modelResponse = text(value?.model ?? value?.message?.model ?? value?.response?.model ?? value?.modelVersion, 128) ?? modelResponse;
      discount = amount(value?.cache_discount) ?? discount;
      if (value.type === 'message_start' || value.type === 'message_delta' || value.type === 'message_stop') {
        if (typeof value.message?.id === 'string') receivedID = value.message.id;
        anth = { ...anth, ...(value.message?.usage ?? {}), ...(value.usage ?? {}) };
        lastUsage = { u: anth, format: 'anthropic' };
        end = value.delta?.stop_reason ?? end;
        if (!isFree) used = provider === 'openrouter' ? usageCost({ usage: anth }, pricing, provider) : anthropicCost(anth, pricing);
        if (value.type === 'message_stop' || (value.type === 'message_delta' && value.delta?.stop_reason != null)) finished = true;
        return;
      }
      if (typeof value.id === 'string') receivedID = value.id;
      else if (typeof value.response?.id === 'string') receivedID = value.response.id;
      if (provider === 'openrouter' && !isFree && !productionWritten && /^gen-[A-Za-z0-9_-]{1,128}$/.test(receivedID)) {
        productionWritten = true;
        productionSave(reservation.id, receivedID);
      }
      if (!isFree && !errorWritten && value?.error && typeof value.error === 'object') {
        errorWritten = true;
        providerError(reservation, value.error, null);
      }
      if (value.choices?.some(c => c.finish_reason != null)) {
        reasoningComplete = value.choices.every(c => ['tool_calls', 'function_call', 'stop'].includes(c.finish_reason));
        end = value.choices.find(c => c.finish_reason != null).finish_reason;
      }
      end = value.candidates?.find(c => c.finishReason)?.finishReason ?? value.response?.status ?? end;
      const usage = value.usage ?? value.usageMetadata ?? value.response?.usage;
      if (usage && typeof usage === 'object') {
        lastUsage = { u: usage, format: value.usageMetadata ? 'usage-metadata' : value.response?.usage ? 'responses' : 'chat' };
      }
      if (!isFree) used = usageCost(value, pricing, provider) ?? used;
      if (value.choices?.some(c => c.finish_reason != null) || value.candidates?.some(c => c.finishReason)
          || value.type === 'response.completed' || value.type === 'response.incomplete') finished = true;
    } catch { invalid = true; }
  };
  const receiptUsage = (durationMs) => Object.fromEntries(Object.entries({
    ...usageSummary(lastUsage?.u, lastUsage?.format), ...(discount !== undefined ? { cache_discount_usd: discount } : {}),
    endpoint: text(endpointName, 64), endpoint_label: endpointName ? extra.orContext?.names?.[endpointName] ?? undefined : undefined, model_response: modelResponse,
    end: text(end, 32), format: lastUsage?.format, price_source: extra.priceSource, price_ts: extra.priceTs, duration_ms: durationMs,
  }).filter(([, v]) => v !== undefined && v !== null));
  const tap = new TransformStream({
    transform(chunk, controller) {
      controller.enqueue(chunk);
      if (invalid) return;
      buffer += decoder.decode(chunk, { stream: true });
      if (buffer.length > 4 * 1024 * 1024) { invalid = true; buffer = ''; return; }
      if (sse) {
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) if (line.startsWith('data:')) item(line.slice(5).trim());
      }
    },
    flush() {
      if (!invalid) {
        buffer += decoder.decode();
        if (!sse) { item(buffer); finished = true; }
        else if (buffer.startsWith('data:')) item(buffer.slice(5).trim());
      }
      if (summaries && finished && !invalid && reasoningComplete) carry.store(session, summaries, model);
      const durationMs = Number.isFinite(extra.begin) ? Math.max(0, Math.round(Date.now() - extra.begin)) : undefined;
      const usage = receiptUsage(durationMs);
      sessionOutcome(extra.orContext, { success: finished && !invalid && end !== 'error', endpointName, output: usage.output, durationMs });
      // What the free bucket measures is the REQUEST COUNT: even if the stream ends broken, the request was made and counts.
      if (isFree) return;
      if (invalid || !finished || used === null) return;
      // B313b reverted: a stuck request had its receipt written, so the synchronous settle did not lock the stream;
      // the asynchronous settle pushed the receipt past the stream close and left readers empty.
      // This transport closure is the request identity, including each retry.
      try {
        ledger({ action: 'settle', id: reservation.id,
          actual: Math.ceil(used),
          receipt: `${reservation.id}:${receivedID || 'complete-response'}`, usage });
      } catch { /* Keep the durable full reservation if receipt recording fails. */ }
    },
  });
  return new Response(response.body.pipeThrough(tap), { status: response.status, statusText: response.statusText, headers: response.headers });
}

export default async function CadreBudget() {
  // a second copy in the same process (cadre-plugin + budget-plugin, live + repo root) does not install the gate again; if it did, the header
  // token would not be found in the other copy and every call would be denied.
  const single = Symbol.for(`cadre.provider-gate:${process.env.CADRE_HOME ?? ''}`);
  const projectTools = process.env.CADRE_PROJECT_TOOLS === '1';
  let toolPolicy;
  if (projectTools) {
    try { toolPolicy = JSON.parse(process.env.CADRE_TOOL_POLICY ?? ''); } catch {}
    if (toolPolicy?.version !== 1 || toolPolicy.sandbox !== '/usr/bin/sandbox-exec'
        || typeof toolPolicy.profile !== 'string' || !toolPolicy.scratch?.startsWith('/')) {
      throw new Error('CADRE_TOOL_DENIED: project sandbox unavailable');
    }
    toolPolicy = Object.freeze(toolPolicy);
    if (toolPolicy.tool !== undefined) {
      let summary = '';
      try { summary = createHash('sha256').update(readFileSync(toolPolicy.tool)).digest('hex'); } catch {}
      if (typeof toolPolicy.tool !== 'string' || !toolPolicy.tool.startsWith('/') || summary !== toolPolicy.tool_sha256) {
        throw new Error('CADRE_TOOL_DENIED: project sandbox wrapper untrusted');
      }
    }
  }
  if (globalThis[single]) {
    if (projectTools && globalThis[single]?.toolPolicy !== process.env.CADRE_TOOL_POLICY) {
      throw new Error('CADRE_TOOL_DENIED: existing provider has no matching project sandbox');
    }
    return {};
  }
  globalThis[single] = projectTools ? { toolPolicy: process.env.CADRE_TOOL_POLICY } : true;
  const shellQuote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
  const toolEnvironment = ['PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'PYTHONPATH', 'VIRTUAL_ENV',
    'CADRE_HOME', 'CADRE_ENGINE_ROOT', 'CADRE_RUN_ID', 'CADRE_TASK_ID']
    .filter((key) => typeof process.env[key] === 'string')
    .map((key) => `${key}=${shellQuote(process.env[key])}`).join(' ');
  const models = new RequestContext();
  const wrappers = new Map();
  return {
    async 'tool.execute.before'(input, output) {
      if (process.env.CADRE_RUN_ID && /^(plan-review-[1-9][0-9]*-[a-f0-9]{8}|review-(author|final|recheck)-[a-f0-9]{12})$/.test(process.env.CADRE_TASK_ID ?? '')) {
        const tool = ledger({ action: 'review-tool' });
        if (tool?.tool_limit) {
          throw new Error(`CADRE_TOOL_LIMIT: ${tool.tool_limit}; do not make new tool calls; give the final answer using available evidence`);
        }
      }
      if (!projectTools) return;
      if (input.tool === 'bash' && typeof output.args?.command === 'string') {
        let command = output.args.command;
        const prefix = toolPolicy.tool ? `exec ${shellQuote(toolPolicy.tool)} ` : null;
        while (prefix && command.startsWith(prefix) && /^'(?:[^']|'\\'')*'$/.test(command.slice(prefix.length))) {
          command = command.slice(prefix.length + 1, -1).replaceAll("'\\''", "'");
        }
        output.args.command = toolPolicy.tool
          ? `${prefix}${shellQuote(command)}`
          : `exec /usr/bin/sandbox-exec -p ${shellQuote(toolPolicy.profile)} /usr/bin/env -i `
            + `${toolEnvironment} TMPDIR=${shellQuote(toolPolicy.scratch)} GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1 `
            + `/bin/sh -c ${shellQuote(output.args.command)}`;
        return;
      }
      if (!['todowrite', 'todoread'].includes(input.tool)) {
        throw new Error('CADRE_TOOL_DENIED: project tools require the confined shell');
      }
    },
    async config(config) {
      // A failed/missing plugin leaves the launcher's local-only allowlist intact.
      if (process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM !== 'false' ||
          !process.env.CADRE_PYTHON || !process.env.CADRE_HOME || !process.env.CADRE_ENGINE_ROOT) {
        // B124: in observation mode a missing install does NOT LOCK manual use; the gate is not installed, calls pass untouched.
        if (observationMode()) return;
        return deny();
      }
      const providers = config.provider ?? {};
      const enabled = [];
      for (const provider of supported) {
        const settings = providers[provider] ?? {};
        const previous = settings.options?.fetch ?? globalThis.fetch;
        const closed = async (url, options = {}) => {
          // Catalogue GETs are unbilled; unknown/multipart/inference transports
          // are not admitted. Redirects must not replay a POST outside the gate.
          if ((options.method ?? 'GET').toUpperCase() === 'GET') return previous(url, options);
          if (typeof options.body !== 'string') return deny(provider, 'unsupported request body');
          let body;
          try { body = JSON.parse(options.body); } catch { return deny(provider, 'request body not json'); }
          const pathModel = String(url).match(/\/models\/([^/:]+):/);
          const name = body.model ?? (pathModel ? decodeURIComponent(pathModel[1]) : null);
          const headers = new Headers(options.headers);
          const entry = models.get(headers.get('x-cadre-budget-context'));
          headers.delete('x-cadre-budget-context');
          const monthlyId = headers.get('x-cadre-monthly-id');
          headers.delete('x-cadre-monthly-id');
          if (!entry || entry.provider !== provider || entry.name !== name) return deny(provider, 'request context missing');
          if (thinkingSet(body, { provider, model: name, role: process.env.CADRE_ROLE, agent: entry.role, url })) {
            options = { ...options, body: JSON.stringify(body) };
          }
          const { total, visible } = requestedOutput(body);   // 16-4: with budget-type thinking, max_tokens − budget is measured
          if (!Number.isSafeInteger(visible) || visible <= 0 || visible > entry.pricing.output) return deny(provider, 'output cap invalid');
          // OR-PIN: ask for the real usage.cost in the response (so the ledger records reality, not the estimate).
          let orContext = null, endpointSelection = null, priceTs;
          if (provider === 'openrouter') carry.add(entry.session, body, name);   // 4c-4: missing reasoning_details are put back on a tool round
          const prefix = prefixMeasure(entry.session, body);
          if (provider === 'openrouter') {
            if (!entry.counted) {
              const prices = orMostExpensive({ id: name }, body, { session: entry.session, proceed: prefix.proceed });
              if (!prices) return deny(provider, 'openrouter fail-closed pricing unavailable');
              orContext = { session: entry.session, entry_name: name, order: prices.providers[0], character: prices.inputTok, names: prices.names,
                        singleEndpoint: prices.providers.length === 1 ? prices.providers[0] : null };
              endpointSelection = { order: prices.providers, reason: prices.choice.reason, ...(prices.choice.eliminated.length ? { eliminated: prices.choice.eliminated } : {}),
                           ...(prices.choice.session ? { session: prices.choice.session } : {}) };
              priceTs = Number.isFinite(prices.priceTs) && prices.priceTs > 0 ? prices.priceTs : undefined;
              // 4b: comes from a closed-weight vendor, its endpoints do not report quantization; the fp8 filter would close it entirely.
              // 4c-3: in a tiered policy the parameter map replaces require_parameters, the maximum price goes in the request.
              body.provider = { only: prices.providers, order: prices.providers, ...(prices.closed ? {} : { quantizations: ['fp8'] }),
                                allow_fallbacks: false, ...(prices.maxPrice ? { max_price: prices.maxPrice } : {}),
                                ...(prices.closed ? {} : { require_parameters: true }) };   // closed weights (Sol/qwen): endpoint parameter map
              entry.pricing = { ...entry.pricing, inRate: prices.input, freshRate: prices.input,
                                readRate: prices.input, writeRate: prices.input, outRate: prices.output };
            } else if (!String(name).endsWith(':free')) {
              return deny(provider, 'openrouter free model identity required');
            }
            body.usage = { include: true };
            options = { ...options, body: JSON.stringify(body) };
          }
          const reservation = ledger({ action: 'reserve', provider: entry.bucket,
            maximum: entry.counted ? 1 : requestQuote(entry.pricing, options.body, total),
            metadata: { model: name, session_id: entry.session, role: entry.role, pricing: entry.pricing, prefix,
                        ...effort(body, provider, url),
                        ...(endpointSelection ? { endpoint_selection: endpointSelection } : {}), ...(monthlyId ? { monthly_id: monthlyId } : {}) } });
          // The reservation is written BEFORE the network. In a paid bucket, if the outcome is unknown the reservation STAYS (to avoid
          // a silent overrun); in a free bucket the request counts whatever the outcome (B83).
          if (process.env.CADRE_REQUEST_SAMPLE) {   // probe/canary: the body leaving the gate goes to a local file (headers and keys are not written)
            try {
              mkdirSync(process.env.CADRE_REQUEST_SAMPLE, { recursive: true });
              writeFileSync(join(process.env.CADRE_REQUEST_SAMPLE, `${Date.now()}-${provider}-${randomUUID().slice(0, 8)}.json`), options.body);
            } catch {}
          }
          let response;
          const begin = Date.now();
          try {
            response = await liveFetch(previous, url, { ...options, headers, redirect: 'error' });
          } catch (error) {
            if (entry.counted) countFreeRequest(reservation, '');
            sessionOutcome(orContext, { success: false });
            throw error;
          }
          // Item 5: 429 = window wall. The stamp file had NEVER BEEN WRITTEN until now (provider_budget.py read it
          // but nobody wrote it), so the "we hit the limit" signal was dead code. It is now written at the limit.
          if (response.status === 429 && provider === 'opencode-go') {
            try {
              mkdirSync(join(process.env.CADRE_HOME, 'chain'), { recursive: true });
              writeFileSync(join(process.env.CADRE_HOME, 'chain', 'go-429-last.txt'), String(Math.floor(Date.now() / 1000)));
            } catch { /* if the stamp cannot be written the call is still denied; the percentage gate works separately */ }
          }
          return receiptStream(response, reservation, entry.pricing, entry.counted, provider, entry.session, name,   // B169: usage denial with provider
            { begin, orContext, priceTs, priceSource: provider === 'openrouter' && !entry.counted ? 'openrouter-endpoints' : 'opencode-catalog' });
        };
        // B124: in observation mode a budget denial does NOT BLOCK the call — the raw call passes, the record stays as attempted.
        const guarded = async (url, options = {}) => {
          try {
            return await closed(url, options);
          } catch (error) {
            if (provider !== 'openrouter' && observationMode() && budgetRejection(error)) return previous(url, options);
            throw error;
          }
        };
        providers[provider] = { ...settings, options: { ...settings.options, fetch: guarded } };
        wrappers.set(provider, guarded);
        enabled.push(provider);
      }
      // B116: thinking level per role (THINKING_ROLE).
      // Hence the role split: MECHANICAL roles want speed -> 'none'; JUDGMENT roles want accuracy -> prefill.
      config.provider = providers;
      config.enabled_providers = enabled; // Last mutation, only after guard installation.
    },
    async 'chat.params'(input, output) {
      const provider = input.model.providerID;
      if (!wrappers.has(provider)) return deny(provider, 'provider not guarded');
      let pricing;
      // Plan is raised to the role cap; other roles clip what opencode asks for to the role cap (invalid request NaN -> denial, B186).
      const planning = input.agent === 'plan' || input.agent === 'cadre-plan' || process.env.CADRE_ROLE === 'plan';
      let limit = enabledOutput(input.model, input.agent);
      if (!Number.isSafeInteger(limit)) return deny(provider, 'model output capacity unknown');
      if (!planning && process.env.CADRE_OUTPUT_MULTIPLIER !== '2' && output.maxOutputTokens !== undefined) limit = Math.min(limit, number(output.maxOutputTokens));
      output.maxOutputTokens = limit;
      if (zeroCost(input.model)) {
        const cap = number(output.maxOutputTokens);
        // B186: number() returns NaN; an invalid limit is denied here, before the provider call.
        if (!Number.isSafeInteger(cap) || cap <= 0) return deny(provider, 'output cap invalid');
        pricing = { output: cap };
      } else {
        pricing = { ...quote(input.model, output.maxOutputTokens), timed: timed(provider, input.model.api?.id ?? input.model.id) };
      }
      output.maxOutputTokens = pricing.output;
    },
    async 'chat.headers'(input, output) {
      const provider = input.model.providerID;
      if (!wrappers.has(provider)) return deny(provider, 'provider not guarded');
      // counted = a bucket that counts REQUESTS instead of dollars: a zero-priced model (zen/Go free models).
      const counted = zeroCost(input.model);
      const modelName = input.model.api?.id ?? input.model.id;
      const bucket = counted ? `${provider}:free` : provider;
      const limit = enabledOutput(input.model, input.agent);
      if (!Number.isSafeInteger(limit)) return deny(provider, 'model output capacity unknown');
      const pricing = counted ? { output: limit } : { ...quote(input.model, limit), timed: timed(provider, modelName) };
      const token = randomUUID();
      models.put(token, { provider, bucket, counted, name: modelName, pricing, session: input.sessionID, role: input.agent });
      output.headers['x-cadre-budget-context'] = token;
    },
  };
}
