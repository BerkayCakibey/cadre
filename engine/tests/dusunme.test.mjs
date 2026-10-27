// 4c-4 / 16(3): the thinking field follows the endpoint's support; high is open for Sol; no field for empty options; level and budget never go together.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as tasi from '../lib/dusunme-tasi.mjs';

const dizin = mkdtempSync(join(tmpdir(), 'qe-dusunme-'));
writeFileSync(join(dizin, 'models.json'), JSON.stringify({
  'opencode-go': { models: {
    'deepseek-v4-pro': { reasoning_options: [{ type: 'effort', values: ['high', 'max'] }] },
    'kimi-k3': { reasoning_options: [{ type: 'effort', values: ['max'] }] },
    'qwen3.8-max': { reasoning_options: [{ type: 'toggle' }, { type: 'effort', values: ['low', 'medium', 'xhigh'] },
                                         { type: 'budget_tokens', max: 262144 }] },
    'duz': { reasoning_options: [] },
  } },
  openrouter: { models: {
    'openai/gpt-6.1-sol': { reasoning_options: [{ type: 'effort', values: ['low', 'medium', 'high', 'xhigh', 'max'] }] },
  } },
}));
process.env.CADRE_MODEL_KATALOG = join(dizin, 'models.json');
const { dusunmeAyarla, gonderilenEfor } = await import('../lib/dusunme.mjs');
const sohbet = 'https://x/v1/chat/completions';

test('reasoning fragments without indices retain payload and metadata as one immutable record', () => {
  const b = tasi.birikim();
  for (const text of ['Türkçe ', 'özet']) tasi.parca(b, { choices: [{ delta: {
    reasoning_details: [{ type: 'reasoning.text', id: 'r1', format: 'fixture', text }],
    tool_calls: [{ id: 'call-unindexed' }]
  } }] });
  assert.equal(tasi.durum(b), 'hazir');
  assert.equal(tasi.sakla('fragment-session', b, 'model-a'), true);
  b.details[0].text = 'mutated accumulator';
  const body = () => ({ messages: [{ role: 'assistant', tool_calls: [{ id: 'call-unindexed' }], reasoning_details: [] }] });
  const first = body();
  assert.equal(tasi.ekle('fragment-session', first, 'model-a'), true);
  assert.deepEqual(first.messages[0].reasoning_details, [{ type: 'reasoning.text', id: 'r1', format: 'fixture', text: 'Türkçe özet' }]);
  first.messages[0].reasoning_details[0].text = 'mutated request';
  const second = body();
  tasi.ekle('fragment-session', second, 'model-a');
  assert.equal(second.messages[0].reasoning_details[0].text, 'Türkçe özet');
});

test('reasoning ownership includes session, model and the complete tool-call identity', () => {
  const b = tasi.birikim();
  tasi.parca(b, { choices: [{ delta: { reasoning_details: [{ type: 'reasoning.encrypted', index: 0, data: 'synthetic' }],
    tool_calls: [{ id: 'call-a' }, { id: 'call-b' }] } }] });
  tasi.sakla('owned-session', b, 'model-a');
  const body = (ids = ['call-b', 'call-a']) => ({ messages: [{ role: 'assistant', tool_calls: ids.map(id => ({ id })) }] });
  assert.equal(tasi.ekle('other-session', body(), 'model-a'), false);
  assert.equal(tasi.ekle('owned-session', body(), 'model-b'), false);
  assert.equal(tasi.ekle('owned-session', body(['call-a']), 'model-a'), false);
  assert.equal(tasi.ekle('owned-session', body(['call-a,call-b']), 'model-a'), false);
  const valid = body();
  assert.equal(tasi.ekle('owned-session', valid, 'model-a'), true);
  valid.messages[0].reasoning_details = [{ type: 'reasoning.text', text: 'client-owned' }];
  assert.equal(tasi.ekle('owned-session', valid, 'model-a'), false);
  assert.equal(valid.messages[0].reasoning_details[0].text, 'client-owned');
});

test('empty, malformed, conflicting and excessive reasoning cannot be cached', () => {
  for (const details of [[], [null], [{ index: -1, type: 'reasoning.text', text: 'x' }],
                        [{ type: 'reasoning.text', text: {} }], [{ type: 'reasoning.text', text: 'x'.repeat(4 * 1024 * 1024 + 1) }]]) {
    const b = tasi.birikim();
    tasi.parca(b, { choices: [{ delta: { reasoning_details: details, tool_calls: [{ id: 'invalid-call' }] } }] });
    assert.equal(tasi.durum(b), details.length ? 'gecersiz' : 'bos');
    assert.equal(tasi.sakla('invalid-session', b), false);
  }
  const conflict = tasi.birikim();
  for (const id of ['first', 'different']) tasi.parca(conflict, { choices: [{ delta: {
    reasoning_details: [{ type: 'reasoning.encrypted', id, index: 0, data: 'fixture' }], tool_calls: [{ id: 'invalid-call' }]
  } }] });
  assert.equal(tasi.durum(conflict), 'gecersiz');
  assert.equal(tasi.sakla('invalid-session', conflict), false);
});

test('reasoning cache evicts old tool turns and sessions at its finite limits', () => {
  for (let i = 0; i < 257; i++) {
    const b = tasi.birikim();
    tasi.parca(b, { choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', text: String(i) }],
      tool_calls: [{ id: 'bounded-' + i }] } }] });
    tasi.sakla('bounded-turns', b, 'model-a');
    tasi.sakla('bounded-session-' + i, b, 'model-a');
  }
  const body = (i) => ({ messages: [{ role: 'assistant', tool_calls: [{ id: 'bounded-' + i }] }] });
  assert.equal(tasi.ekle('bounded-turns', body(0), 'model-a'), false);
  assert.equal(tasi.ekle('bounded-turns', body(256), 'model-a'), true);
  assert.equal(tasi.ekle('bounded-session-0', body(0), 'model-a'), false);
  assert.equal(tasi.ekle('bounded-session-256', body(256), 'model-a'), true);
});

test('plan and judge ask for high; without it in the catalogue, the nearest allowed value', () => {
  const b = {};
  assert.equal(dusunmeAyarla(b, { provider: 'opencode-go', model: 'deepseek-v4-pro', rol: 'plan', ajan: 'build', url: sohbet }), true);
  assert.equal(b.reasoning_effort, 'high');
  const k = {};
  dusunmeAyarla(k, { provider: 'opencode-go', model: 'kimi-k3', rol: 'kalite', ajan: 'build', url: sohbet });
  assert.equal(k.reasoning_effort, 'max', 'kimi yalniz max bilir');
});

test('in the lane role an invalid value drops to the nearest catalogue value; a valid value is left untouched', () => {
  const b = { reasoning_effort: 'medium' };
  dusunmeAyarla(b, { provider: 'opencode-go', model: 'deepseek-v4-pro', rol: 'impl', ajan: 'build', url: sohbet });
  assert.equal(b.reasoning_effort, 'high', 'DS-pro yalniz high/max');
  const c = { reasoning_effort: 'max' };
  assert.equal(dusunmeAyarla(c, { provider: 'opencode-go', model: 'deepseek-v4-pro', rol: 'impl', ajan: 'build', url: sohbet }), false);
});

test('Sol gets explicit high in every role, never none; the OR form is a reasoning object and the budget is dropped', () => {
  const b = { reasoning: { effort: 'none', max_tokens: 4000 } };
  dusunmeAyarla(b, { provider: 'openrouter', model: 'openai/gpt-6.1-sol', rol: 'impl', ajan: 'title', url: sohbet });
  assert.deepEqual(b.reasoning, { effort: 'high' });
  assert.equal(b.temperature, undefined);
});

test('no field is sent when the option list is empty or the model is not in the catalogue', () => {
  for (const model of ['duz', 'bilinmeyen']) {
    const b = { reasoning_effort: 'high' };
    dusunmeAyarla(b, { provider: 'opencode-go', model, rol: 'plan', ajan: 'build', url: sohbet });
    assert.equal(b.reasoning_effort, undefined, model);
  }
});

test('Anthropic form (Go qwen): the mode is pinned to effort, the budget is removed, max_tokens drops to the visible cap', () => {
  const plan = { thinking: { type: 'enabled', budget_tokens: 2000 }, max_tokens: 34000 };
  assert.equal(dusunmeAyarla(plan, { provider: 'opencode-go', model: 'qwen3.8-max', rol: 'plan', ajan: 'build',
    url: 'https://x/v1/messages' }), true);
  assert.deepEqual([plan.output_config, plan.thinking, plan.max_tokens], [{ effort: 'xhigh' }, undefined, 32000], 'high yok: en yakin xhigh');
  const serit = { thinking: { type: 'enabled', budget_tokens: 2000 }, max_tokens: 34000 };
  dusunmeAyarla(serit, { provider: 'opencode-go', model: 'qwen3.8-max', rol: 'impl', ajan: 'build', url: 'https://x/v1/messages' });
  assert.deepEqual([serit.output_config, serit.thinking], [{ effort: 'medium' }, undefined]);
  const effort = { output_config: { effort: 'low' }, max_tokens: 32000 };
  assert.equal(dusunmeAyarla(effort, { provider: 'opencode-go', model: 'qwen3.8-max', rol: 'impl', ajan: 'title',
    url: 'https://x/v1/messages' }), false, 'gecerli effort dokunulmaz');
});

test('4c-10: compaction runs on the model of the lane at the lowest level it supports (overriding the Sol rule too)', () => {
  const q = { reasoning_effort: 'xhigh' };
  dusunmeAyarla(q, { provider: 'opencode-go', model: 'qwen3.8-max', rol: 'impl', ajan: 'compaction', url: sohbet });
  assert.equal(q.reasoning_effort, 'low');
  const d = {};
  dusunmeAyarla(d, { provider: 'opencode-go', model: 'deepseek-v4-pro', rol: 'impl', ajan: 'compaction', url: sohbet });
  assert.equal(d.reasoning_effort, 'high', 'DS-pro en dusuk high bilir');
  const s = {};
  dusunmeAyarla(s, { provider: 'openrouter', model: 'openai/gpt-6.1-sol', rol: 'kalite', ajan: 'compaction', url: sohbet });
  assert.equal(s.reasoning?.effort, 'low');
});

test('CR-50: opt-in economic effort applies only to the implementation lane, maps to supported levels and never weakens plan or judge', () => {
  const onceki = { efor: process.env.CADRE_EFOR, politika: process.env.CADRE_EFOR_POLITIKASI };
  try {
    const ayarla = (model, rol, ajan = 'build', govde = {}) => {
      const body = { max_tokens: 100, ...govde };
      dusunmeAyarla(body, { provider: 'opencode-go', model, rol, ajan, url: sohbet });
      return gonderilenEfor(body, 'opencode-go', sohbet);
    };
    delete process.env.CADRE_EFOR;
    assert.equal(ayarla('qwen3.8-max', 'impl'), undefined, 'politika kapaliyken saglayici varsayilani korunur');
    process.env.CADRE_EFOR = 'low';
    process.env.CADRE_EFOR_POLITIKASI = 'ekonomik-efor-v1';
    assert.equal(ayarla('qwen3.8-max', 'impl'), 'low');
    assert.equal(ayarla('deepseek-v4-pro', 'impl'), 'high', 'desteklenmeyen dusuk efor en yakin destekli seviyeye cikar');
    assert.equal(ayarla('duz', 'impl', 'build', { reasoning_effort: 'low' }), undefined, 'efor desteklemeyen uca alan gitmez');
    assert.equal(ayarla('qwen3.8-max', 'plan', 'cadre-plan'), 'xhigh', 'guclu plan korunur');
    assert.equal(ayarla('qwen3.8-max', 'kalite', 'build'), 'xhigh', 'bagimsiz son hakem korunur');
    assert.equal(ayarla('qwen3.8-max', 'qa', 'build'), undefined, 'onarim/QA seridi ekonomik efora zorlanmaz');
    process.env.CADRE_EFOR = 'gecersiz';
    assert.equal(ayarla('qwen3.8-max', 'impl'), undefined, 'taninmayan seviye uygulanmaz');
  } finally {
    if (onceki.efor === undefined) delete process.env.CADRE_EFOR; else process.env.CADRE_EFOR = onceki.efor;
    if (onceki.politika === undefined) delete process.env.CADRE_EFOR_POLITIKASI; else process.env.CADRE_EFOR_POLITIKASI = onceki.politika;
  }
});
