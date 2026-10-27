// Shared monthly cash ceiling for paid Zen and OpenRouter calls (Go is a subscription pool: 4c-13).
// Provider-specific admission remains in butce-plugin.mjs; this is an additional
// automation-wide fail-closed guard.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { rolCiktisi, istenenCikti } from './sinirlar.mjs';
import { IstekBaglami } from './istek-baglami.mjs';
const targeted = new Set(['opencode', 'openrouter']);
const contexts = new IstekBaglami();   // 4c-4: the token is not single-use; the header agent's SDK retry passes
const gozlemKipi = () => process.env.CADRE_BUTCE_GOZLEM === '1' && !process.env.CADRE_RUN_ID;
const budgetDenied = (error) => String(error?.message ?? '').startsWith('CADRE_BUDGET_DENIED');

const number = (value) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return Number.NaN;
  return value;
};
const integer = (value, fallback = 0) => Number.isSafeInteger(value) && value >= 0 ? value : fallback;

function monthly(request) {
  try {
    return JSON.parse(execFileSync(process.env.CADRE_PYTHON,
      [join(process.env.CADRE_ENGINE_ROOT, 'lib/aylik_butce.py')], {
        input: JSON.stringify(request), encoding: 'utf8', timeout: 45000, maxBuffer: 65536,
        stdio: ['pipe', 'pipe', 'pipe'],
      }));
  } catch (error) {
    throw new Error(`CADRE_BUDGET_DENIED: monthly paid budget unavailable${error?.status ? ` (${error.status})` : ''}`);
  }
}

function zeroCost(model) {
  const cost = model?.cost;
  return !!cost && typeof cost.input === 'number' && typeof cost.output === 'number'
    && cost.input === 0 && cost.output === 0;
}

function orMostExpensive(model) {
  const name = String(model?.api?.id ?? model?.id ?? '').replace(/^openrouter\//, '');
  if (!name) return null;
  try {
    const data = JSON.parse(readFileSync(join(process.env.CADRE_HOME ?? '', 'zincir', 'openrouter-fiyat.json'), 'utf8'));
    const row = (data?.modeller ?? {})[name];
    const input = number(row?.input), output = number(row?.output);
    if (!Number.isFinite(input) || !Number.isFinite(output) || input <= 0 || output <= 0) return null;
    return { input, output };
  } catch { return null; }
}

function quote(model, maximum) {
  const provider = model?.providerID ?? null;
  const input = number(model?.limit?.input ?? model?.limit?.context);
  const output = Math.min(number(model?.limit?.output), number(maximum));
  if (!Number.isSafeInteger(input) || !Number.isSafeInteger(output) || input <= 0 || output <= 0) {
    throw new Error('CADRE_BUDGET_DENIED: monthly pricing unknown');
  }
  if (provider === 'openrouter') {
    const expensive = orMostExpensive(model);
    if (!expensive) throw new Error('CADRE_BUDGET_DENIED: monthly OpenRouter pricing unavailable');
    return { input, output, inRate: expensive.input, freshRate: expensive.input,
      readRate: expensive.input, writeRate: expensive.input, outRate: expensive.output };
  }
  const costs = [model?.cost, model?.cost?.context_over_200k].filter(Boolean);
  const freshRate = Math.max(...costs.map(c => number(c.input)));
  const writeRate = Math.max(...costs.map(c => number(c.cache?.write ?? c.cache_write ?? 0)));
  const reads = costs.map(c => c.cache?.read ?? c.cache_read).filter(v => typeof v === 'number');
  const readRate = reads.length ? Math.max(...reads.map(number)) : freshRate;
  const inRate = Math.max(freshRate, writeRate, readRate);
  const outRate = Math.max(...costs.map(c => number(c.output)));
  if (!Number.isFinite(inRate) || !Number.isFinite(outRate) || inRate <= 0 || outRate <= 0) {
    throw new Error('CADRE_BUDGET_DENIED: monthly pricing unknown');
  }
  return { input, output, inRate, freshRate, readRate, writeRate, outRate };
}

function requestQuote(pricing, rawBody, output = pricing.output) {
  const chars = number(rawBody.length);
  const inputTokens = Math.min(pricing.input, chars + 4096);
  return Math.ceil(inputTokens * pricing.inRate + Math.max(pricing.output, output) * pricing.outRate);
}

function anthropicCost(usage, pricing) {
  const fresh = usage?.input_tokens, read = usage?.cache_read_input_tokens ?? 0;
  const write = usage?.cache_creation_input_tokens ?? 0, output = usage?.output_tokens;
  if (![fresh, read, write, output].every(v => Number.isSafeInteger(v) && v >= 0)) return null;
  return Math.ceil(fresh * (pricing.freshRate ?? pricing.inRate)
    + read * (pricing.readRate ?? pricing.freshRate ?? pricing.inRate)
    + write * (pricing.writeRate || pricing.freshRate || pricing.inRate)
    + output * pricing.outRate);
}

function usageCost(value, pricing, provider) {
  const usage = value?.usage ?? value?.usageMetadata ?? value?.response?.usage;
  if (!usage) return null;
  if (provider === 'openrouter') {
    return typeof usage.cost === 'number' && Number.isFinite(usage.cost) && usage.cost >= 0
      && Number.isSafeInteger(Math.ceil(usage.cost * 1e6)) ? Math.ceil(usage.cost * 1e6) : null;
  }
  if (value.type === 'message') return anthropicCost(usage, pricing);
  const input = number(usage.prompt_tokens ?? usage.promptTokenCount ?? usage.input_tokens);
  const output = number(usage.completion_tokens ?? usage.candidatesTokenCount ?? usage.output_tokens);
  const reasoning = usage.completion_tokens === undefined ? number(usage.thoughtsTokenCount ?? 0) : 0;
  if (![input, output, reasoning].every(Number.isSafeInteger)) return null;
  const read = integer(usage.prompt_tokens_details?.cached_tokens ?? usage.input_tokens_details?.cached_tokens
    ?? usage.cache_read_input_tokens ?? usage.cachedContentTokenCount);
  const write = integer(usage.cache_creation_input_tokens ?? usage.prompt_tokens_details?.cache_creation_tokens);
  const fresh = Math.max(0, input - Math.min(input, read + write));
  const readCount = Math.min(input, read);
  const writeCount = Math.min(Math.max(0, input - readCount), write);
  return Math.ceil(fresh * (pricing.freshRate ?? pricing.inRate)
    + readCount * (pricing.readRate ?? pricing.freshRate ?? pricing.inRate)
    + writeCount * (pricing.writeRate || pricing.freshRate || pricing.inRate)
    + (output + reasoning) * pricing.outRate);
}

function settleZero(reservation, reason) {
  try { monthly({ action: 'settle', id: reservation.id, actual: 0,
    receipt: `${reservation.id}:${reason}` }); } catch { /* fail closed: keep the hold */ }
}

function receiptStream(response, reservation, pricing, provider) {
  if (response.status === 429) { settleZero(reservation, 'http-429'); return response; }   // request not processed (4c-4)
  if (!response.ok || !response.body) return response;
  const sse = (response.headers.get('content-type') ?? '').includes('text/event-stream');
  const decoder = new TextDecoder();
  let buffer = '', used = null, finished = false, invalid = false, receivedID = '', anth = null;
  const item = (raw) => {
    if (raw === '[DONE]') { finished = true; return; }
    try {
      const value = JSON.parse(raw);
      if (value.type === 'message_start' || value.type === 'message_delta' || value.type === 'message_stop') {
        if (typeof value.message?.id === 'string') receivedID = value.message.id;
        anth = { ...anth, ...(value.message?.usage ?? {}), ...(value.usage ?? {}) };
        used = anthropicCost(anth, pricing) ?? used;
        if (value.type === 'message_stop' || (value.type === 'message_delta' && value.delta?.stop_reason != null)) finished = true;
        return;
      }
      if (typeof value.id === 'string') receivedID = value.id;
      else if (typeof value.response?.id === 'string') receivedID = value.response.id;
      used = usageCost(value, pricing, provider) ?? used;
      if (value.choices?.some(c => c.finish_reason != null) || value.candidates?.some(c => c.finishReason)
          || value.type === 'response.completed' || value.type === 'response.incomplete') finished = true;
    } catch { invalid = true; }
  };
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
      if (invalid || !finished || used === null) return;
      try {
        monthly({ action: 'settle', id: reservation.id,
          actual: Math.ceil(used),
          receipt: `${reservation.id}:${receivedID || 'complete-response'}` });
      } catch { /* unknown settlement keeps the full hold */ }
    },
  });
  return new Response(response.body.pipeThrough(tap), {
    status: response.status, statusText: response.statusText, headers: response.headers,
  });
}

export default async function MonthlyBudget() {
  const tekil = Symbol.for(`cadre.aylik-kapi:${process.env.CADRE_HOME ?? ''}`);   // 07.10: same uniqueness as the provider gate
  if (globalThis[tekil]) return {};
  globalThis[tekil] = true;
  const wrappers = new Map();
  return {
    async config(config) {
      if (!process.env.CADRE_PYTHON || !process.env.CADRE_HOME || !process.env.CADRE_ENGINE_ROOT) {
        if (gozlemKipi()) return;
        throw new Error('CADRE_BUDGET_DENIED: monthly budget guard unavailable');
      }
      const providers = config.provider ?? {};
      for (const provider of targeted) {
        const settings = providers[provider] ?? {};
        const previous = settings.options?.fetch ?? globalThis.fetch;
        const guarded = async (url, options = {}) => {
          if ((options.method ?? 'GET').toUpperCase() === 'GET') return previous(url, options);
          if (typeof options.body !== 'string') throw new Error('CADRE_BUDGET_DENIED: monthly request body unavailable');
          let body;
          try { body = JSON.parse(options.body); } catch { throw new Error('CADRE_BUDGET_DENIED: monthly request body invalid'); }
          const pathModel = String(url).match(/\/models\/([^/:]+):/);
          const name = body.model ?? (pathModel ? decodeURIComponent(pathModel[1]) : null);
          const headers = new Headers(options.headers);
          const token = headers.get('x-cadre-monthly-budget-context');
          const entry = contexts.al(token);
          headers.delete('x-cadre-monthly-budget-context');
          if (!entry || entry.provider !== provider || entry.name !== name) {
            throw new Error('CADRE_BUDGET_DENIED: monthly request context missing');
          }
          if (!entry.paid) return previous(url, { ...options, headers });
          const { toplam, gorunur } = istenenCikti(body);
          if (!Number.isSafeInteger(gorunur) || gorunur <= 0 || gorunur > entry.pricing.output) {
            throw new Error('CADRE_BUDGET_DENIED: monthly output cap invalid');
          }
          if (provider === 'openrouter' && body.usage === undefined) {
            body.usage = { include: true };
            options = { ...options, body: JSON.stringify(body) };
          }
          const reservation = monthly({ action: 'reserve', provider,
            maximum: requestQuote(entry.pricing, options.body, toplam),
            metadata: { model: name, session_id: entry.session, role: entry.role } });
          if (previous !== globalThis.fetch) headers.set('x-cadre-aylik-kimlik', String(reservation.id));
          let response;
          try {
            response = await previous(url, { ...options, headers });
          } catch (error) {
            // The inner provider gate's CADRE_BUDGET_DENIED occurs before network I/O.
            // In that one proven case the monthly hold can be released safely.
            if (budgetDenied(error)) settleZero(reservation, 'provider-budget-denied');
            throw error;
          }
          return receiptStream(response, reservation, entry.pricing, provider);
        };
        providers[provider] = { ...settings, options: { ...(settings.options ?? {}), fetch: guarded } };
        wrappers.set(provider, guarded);
      }
      config.provider = providers;
    },
    async 'chat.headers'(input, output) {
      const provider = input.model.providerID;
      if (!targeted.has(provider)) return;
      if (!wrappers.has(provider)) throw new Error('CADRE_BUDGET_DENIED: monthly provider not guarded');
      const paid = !zeroCost(input.model);
      const name = input.model.api?.id ?? input.model.id;
      const pricing = paid ? quote(input.model, rolCiktisi(process.env.CADRE_ROL ?? '', input.agent)) : null;
      const token = randomUUID();
      contexts.koy(token, { provider, paid, name, pricing, session: input.sessionID, role: input.agent });
      output.headers['x-cadre-monthly-budget-context'] = token;
    },
  };
}
