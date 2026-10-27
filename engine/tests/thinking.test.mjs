// 4c-4 / 16(3): the thinking field follows the endpoint's support; high is open for Sol; no field for empty options; level and budget never go together.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as carry from '../lib/thinking-carry.mjs';

const directory = mkdtempSync(join(tmpdir(), 'qe-thinking-'));
writeFileSync(join(directory, 'models.json'), JSON.stringify({
  'opencode-go': { models: {
    'deepseek-v4-pro': { reasoning_options: [{ type: 'effort', values: ['high', 'max'] }] },
    'kimi-k3': { reasoning_options: [{ type: 'effort', values: ['max'] }] },
    'qwen3.8-max': { reasoning_options: [{ type: 'toggle' }, { type: 'effort', values: ['low', 'medium', 'xhigh'] },
                                         { type: 'budget_tokens', max: 262144 }] },
    'plain': { reasoning_options: [] },
  } },
  openrouter: { models: {
    'openai/gpt-6.1-sol': { reasoning_options: [{ type: 'effort', values: ['low', 'medium', 'high', 'xhigh', 'max'] }] },
  } },
}));
process.env.CADRE_MODEL_CATALOG = join(directory, 'models.json');
const { thinkingSet, sentEffort } = await import('../lib/thinking.mjs');
const chat = 'https://x/v1/chat/completions';

test('reasoning fragments without indices retain payload and metadata as one immutable record', () => {
  const b = carry.accumulation();
  for (const text of ['Résumé ', 'summary']) carry.part(b, { choices: [{ delta: {
    reasoning_details: [{ type: 'reasoning.text', id: 'r1', format: 'fixture', text }],
    tool_calls: [{ id: 'call-unindexed' }]
  } }] });
  assert.equal(carry.status(b), 'ready');
  assert.equal(carry.store('fragment-session', b, 'model-a'), true);
  b.details[0].text = 'mutated accumulator';
  const body = () => ({ messages: [{ role: 'assistant', tool_calls: [{ id: 'call-unindexed' }], reasoning_details: [] }] });
  const first = body();
  assert.equal(carry.add('fragment-session', first, 'model-a'), true);
  assert.deepEqual(first.messages[0].reasoning_details, [{ type: 'reasoning.text', id: 'r1', format: 'fixture', text: 'Résumé summary' }]);
  first.messages[0].reasoning_details[0].text = 'mutated request';
  const second = body();
  carry.add('fragment-session', second, 'model-a');
  assert.equal(second.messages[0].reasoning_details[0].text, 'Résumé summary');
});

test('reasoning ownership includes session, model and the complete tool-call identity', () => {
  const b = carry.accumulation();
  carry.part(b, { choices: [{ delta: { reasoning_details: [{ type: 'reasoning.encrypted', index: 0, data: 'synthetic' }],
    tool_calls: [{ id: 'call-a' }, { id: 'call-b' }] } }] });
  carry.store('owned-session', b, 'model-a');
  const body = (ids = ['call-b', 'call-a']) => ({ messages: [{ role: 'assistant', tool_calls: ids.map(id => ({ id })) }] });
  assert.equal(carry.add('other-session', body(), 'model-a'), false);
  assert.equal(carry.add('owned-session', body(), 'model-b'), false);
  assert.equal(carry.add('owned-session', body(['call-a']), 'model-a'), false);
  assert.equal(carry.add('owned-session', body(['call-a,call-b']), 'model-a'), false);
  const valid = body();
  assert.equal(carry.add('owned-session', valid, 'model-a'), true);
  valid.messages[0].reasoning_details = [{ type: 'reasoning.text', text: 'client-owned' }];
  assert.equal(carry.add('owned-session', valid, 'model-a'), false);
  assert.equal(valid.messages[0].reasoning_details[0].text, 'client-owned');
});

test('empty, malformed, conflicting and excessive reasoning cannot be cached', () => {
  for (const details of [[], [null], [{ index: -1, type: 'reasoning.text', text: 'x' }],
                        [{ type: 'reasoning.text', text: {} }], [{ type: 'reasoning.text', text: 'x'.repeat(4 * 1024 * 1024 + 1) }]]) {
    const b = carry.accumulation();
    carry.part(b, { choices: [{ delta: { reasoning_details: details, tool_calls: [{ id: 'invalid-call' }] } }] });
    assert.equal(carry.status(b), details.length ? 'invalid' : 'empty');
    assert.equal(carry.store('invalid-session', b), false);
  }
  const conflict = carry.accumulation();
  for (const id of ['first', 'different']) carry.part(conflict, { choices: [{ delta: {
    reasoning_details: [{ type: 'reasoning.encrypted', id, index: 0, data: 'fixture' }], tool_calls: [{ id: 'invalid-call' }]
  } }] });
  assert.equal(carry.status(conflict), 'invalid');
  assert.equal(carry.store('invalid-session', conflict), false);
});

test('reasoning cache evicts old tool turns and sessions at its finite limits', () => {
  for (let i = 0; i < 257; i++) {
    const b = carry.accumulation();
    carry.part(b, { choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', text: String(i) }],
      tool_calls: [{ id: 'bounded-' + i }] } }] });
    carry.store('bounded-turns', b, 'model-a');
    carry.store('bounded-session-' + i, b, 'model-a');
  }
  const body = (i) => ({ messages: [{ role: 'assistant', tool_calls: [{ id: 'bounded-' + i }] }] });
  assert.equal(carry.add('bounded-turns', body(0), 'model-a'), false);
  assert.equal(carry.add('bounded-turns', body(256), 'model-a'), true);
  assert.equal(carry.add('bounded-session-0', body(0), 'model-a'), false);
  assert.equal(carry.add('bounded-session-256', body(256), 'model-a'), true);
});

test('plan and judge ask for high; without it in the catalogue, the nearest allowed value', () => {
  const b = {};
  assert.equal(thinkingSet(b, { provider: 'opencode-go', model: 'deepseek-v4-pro', role: 'plan', agent: 'build', url: chat }), true);
  assert.equal(b.reasoning_effort, 'high');
  const k = {};
  thinkingSet(k, { provider: 'opencode-go', model: 'kimi-k3', role: 'quality', agent: 'build', url: chat });
  assert.equal(k.reasoning_effort, 'max', 'Kimi supports only max');
});

test('in the lane role an invalid value drops to the nearest catalogue value; a valid value is left untouched', () => {
  const b = { reasoning_effort: 'medium' };
  thinkingSet(b, { provider: 'opencode-go', model: 'deepseek-v4-pro', role: 'impl', agent: 'build', url: chat });
  assert.equal(b.reasoning_effort, 'high', 'DS-pro supports only high and max');
  const c = { reasoning_effort: 'max' };
  assert.equal(thinkingSet(c, { provider: 'opencode-go', model: 'deepseek-v4-pro', role: 'impl', agent: 'build', url: chat }), false);
});

test('Sol gets explicit high in every role, never none; the OR form is a reasoning object and the budget is dropped', () => {
  const b = { reasoning: { effort: 'none', max_tokens: 4000 } };
  thinkingSet(b, { provider: 'openrouter', model: 'openai/gpt-6.1-sol', role: 'impl', agent: 'title', url: chat });
  assert.deepEqual(b.reasoning, { effort: 'high' });
  assert.equal(b.temperature, undefined);
});

test('no field is sent when the option list is empty or the model is not in the catalogue', () => {
  for (const model of ['plain', 'unknown']) {
    const b = { reasoning_effort: 'high' };
    thinkingSet(b, { provider: 'opencode-go', model, role: 'plan', agent: 'build', url: chat });
    assert.equal(b.reasoning_effort, undefined, model);
  }
});

test('Anthropic form (Go qwen): the mode is pinned to effort, the budget is removed, max_tokens drops to the visible cap', () => {
  const plan = { thinking: { type: 'enabled', budget_tokens: 2000 }, max_tokens: 34000 };
  assert.equal(thinkingSet(plan, { provider: 'opencode-go', model: 'qwen3.8-max', role: 'plan', agent: 'build',
    url: 'https://x/v1/messages' }), true);
  assert.deepEqual([plan.output_config, plan.thinking, plan.max_tokens], [{ effort: 'xhigh' }, undefined, 32000], 'High is absent; xhigh is the nearest option');
  const lane = { thinking: { type: 'enabled', budget_tokens: 2000 }, max_tokens: 34000 };
  thinkingSet(lane, { provider: 'opencode-go', model: 'qwen3.8-max', role: 'impl', agent: 'build', url: 'https://x/v1/messages' });
  assert.deepEqual([lane.output_config, lane.thinking], [{ effort: 'medium' }, undefined]);
  const effort = { output_config: { effort: 'low' }, max_tokens: 32000 };
  assert.equal(thinkingSet(effort, { provider: 'opencode-go', model: 'qwen3.8-max', role: 'impl', agent: 'title',
    url: 'https://x/v1/messages' }), false, 'valid effort is preserved');
});

test('4c-10: compaction runs on the model of the lane at the lowest level it supports (overriding the Sol rule too)', () => {
  const q = { reasoning_effort: 'xhigh' };
  thinkingSet(q, { provider: 'opencode-go', model: 'qwen3.8-max', role: 'impl', agent: 'compaction', url: chat });
  assert.equal(q.reasoning_effort, 'low');
  const d = {};
  thinkingSet(d, { provider: 'opencode-go', model: 'deepseek-v4-pro', role: 'impl', agent: 'compaction', url: chat });
  assert.equal(d.reasoning_effort, 'high', 'High is the lowest DS-pro effort');
  const s = {};
  thinkingSet(s, { provider: 'openrouter', model: 'openai/gpt-6.1-sol', role: 'quality', agent: 'compaction', url: chat });
  assert.equal(s.reasoning?.effort, 'low');
});

test('CR-50: opt-in economic effort applies only to the implementation lane, maps to supported levels and never weakens plan or judge', () => {
  const previous = { effort: process.env.CADRE_EFFORT, policy: process.env.CADRE_EFFORT_POLICY };
  try {
    const set = (model, role, agent = 'build', extraBody = {}) => {
      const body = { max_tokens: 100, ...extraBody };
      thinkingSet(body, { provider: 'opencode-go', model, role, agent, url: chat });
      return sentEffort(body, 'opencode-go', chat);
    };
    delete process.env.CADRE_EFFORT;
    assert.equal(set('qwen3.8-max', 'impl'), undefined, 'the provider default is preserved when policy is disabled');
    process.env.CADRE_EFFORT = 'low';
    process.env.CADRE_EFFORT_POLICY = 'economic-effort-v1';
    assert.equal(set('qwen3.8-max', 'impl'), 'low');
    assert.equal(set('deepseek-v4-pro', 'impl'), 'high', 'Map unsupported low effort to the nearest supported level');
    assert.equal(set('plain', 'impl', 'build', { reasoning_effort: 'low' }), undefined, 'an endpoint without effort support receives no effort field');
    assert.equal(set('qwen3.8-max', 'plan', 'cadre-plan'), 'xhigh', 'strong planning effort is preserved');
    assert.equal(set('qwen3.8-max', 'quality', 'build'), 'xhigh', 'independent last judge preserved');
    assert.equal(set('qwen3.8-max', 'qa', 'build'), undefined, 'the repair/QA lane is not forced to use economic effort');
    process.env.CADRE_EFFORT = 'invalid';
    assert.equal(set('qwen3.8-max', 'impl'), undefined, 'an unknown level is not applied');
  } finally {
    if (previous.effort === undefined) delete process.env.CADRE_EFFORT; else process.env.CADRE_EFFORT = previous.effort;
    if (previous.policy === undefined) delete process.env.CADRE_EFFORT_POLICY; else process.env.CADRE_EFFORT_POLICY = previous.policy;
  }
});
