// 4c-4: in an OpenRouter tool loop the reasoning summary (reasoning_details) is put back into the assistant message of the next request. OR
// recommends this and some models return 400 when it is missing; if the client does not carry it, the plugin keeps it per session and adds it only where missing.
const REPO = new Map();   // session -> Map(tool call ids -> details)
const CAP = 256;

export function accumulation() { return { details: [], tools: new Set(), keys: new Map(), invalid: false, bytes: 0 }; }

export function status(b) {
  if (b.invalid) return 'invalid';
  return b.details.some((row) => ['text', 'summary', 'data'].some((field) => typeof row[field] === 'string' && row[field].length))
    ? 'ready' : 'empty';
}

// Stream chunks deliver a summary with the same `index` in pieces: text fields are joined, other fields keep the first value seen.
export function part(b, value) {
  if (value?.choices == null) return;
  if (!Array.isArray(value.choices) || value.choices.length > 1) { b.invalid = true; return; }
  for (const choice of value?.choices ?? []) {
    if (choice?.index != null && choice.index !== 0) { b.invalid = true; return; }
    const d = choice?.delta ?? choice?.message ?? {};
    if (d.reasoning_details != null && !Array.isArray(d.reasoning_details)) { b.invalid = true; return; }
    for (const [order, summary] of (Array.isArray(d.reasoning_details) ? d.reasoning_details : []).entries()) {
      if (!summary || typeof summary.type !== 'string' || !summary.type
          || (summary.index != null && (!Number.isSafeInteger(summary.index) || summary.index < 0))) { b.invalid = true; return; }
      const slot = summary?.index ?? order;
      const key = JSON.stringify([slot, summary.type]);
      const old = b.keys.get(key);
      for (const field of ['text', 'summary', 'data']) {
        if (summary[field] != null && typeof summary[field] !== 'string') { b.invalid = true; return; }
        b.bytes += new TextEncoder().encode(summary[field] ?? '').byteLength;
      }
      if (b.bytes > 4 * 1024 * 1024) { b.invalid = true; return; }
      if (!old) {
        const row = structuredClone(summary);
        b.details.push(row);
        b.keys.set(key, row);
        continue;
      }
      if (['id', 'format'].some((field) => old[field] != null && summary[field] != null && old[field] !== summary[field])) {
        b.invalid = true; return;
      }
      for (const field of ['text', 'summary', 'data']) {
        if (typeof summary?.[field] === 'string') old[field] = (old[field] ?? '') + summary[field];
      }
    }
    for (const tool of d.tool_calls ?? []) if (typeof tool?.id === 'string') b.tools.add(tool.id);
  }
}

export function store(session, b, model = '') {
  if (!session || status(b) !== 'ready' || !b.tools.size) return false;
  const identity = JSON.stringify([session, model]);
  const record = REPO.get(identity) ?? new Map();
  record.set(JSON.stringify([...b.tools].sort()), structuredClone(b.details));
  while (record.size > CAP) record.delete(record.keys().next().value);
  REPO.delete(identity);
  REPO.set(identity, record);
  while (REPO.size > CAP) REPO.delete(REPO.keys().next().value);
  return true;
}

// Returns: true if the body changed.
export function add(session, body, model = '') {
  const record = REPO.get(JSON.stringify([session, model]));
  if (!record || !Array.isArray(body?.messages)) return false;
  let changed = false;
  for (const message of body.messages) {
    if (message?.role !== 'assistant' || !Array.isArray(message.tool_calls)
        || (message.reasoning_details != null && (!Array.isArray(message.reasoning_details) || message.reasoning_details.length))) continue;
    const details = record.get(JSON.stringify(message.tool_calls.map((t) => t?.id).filter(Boolean).sort()));
    if (details) { message.reasoning_details = structuredClone(details); changed = true; }
  }
  return changed;
}
