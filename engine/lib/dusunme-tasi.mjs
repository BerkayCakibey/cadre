// 4c-4: in an OpenRouter tool loop the reasoning summary (reasoning_details) is put back into the assistant message of the next request. OR
// recommends this and some models return 400 when it is missing; if the client does not carry it, the plugin keeps it per session and adds it only where missing.
const DEPO = new Map();   // session -> Map(tool call ids -> details)
const TAVAN = 256;

export function birikim() { return { details: [], araclar: new Set(), anahtarlar: new Map(), invalid: false, bytes: 0 }; }

export function durum(b) {
  if (b.invalid) return 'gecersiz';
  return b.details.some((row) => ['text', 'summary', 'data'].some((field) => typeof row[field] === 'string' && row[field].length))
    ? 'hazir' : 'bos';
}

// Stream chunks deliver a summary with the same `index` in pieces: text fields are joined, other fields keep the first value seen.
export function parca(b, value) {
  if (value?.choices == null) return;
  if (!Array.isArray(value.choices) || value.choices.length > 1) { b.invalid = true; return; }
  for (const secim of value?.choices ?? []) {
    if (secim?.index != null && secim.index !== 0) { b.invalid = true; return; }
    const d = secim?.delta ?? secim?.message ?? {};
    if (d.reasoning_details != null && !Array.isArray(d.reasoning_details)) { b.invalid = true; return; }
    for (const [sira, ozet] of (Array.isArray(d.reasoning_details) ? d.reasoning_details : []).entries()) {
      if (!ozet || typeof ozet.type !== 'string' || !ozet.type
          || (ozet.index != null && (!Number.isSafeInteger(ozet.index) || ozet.index < 0))) { b.invalid = true; return; }
      const anahtar = ozet?.index ?? sira;
      const key = JSON.stringify([anahtar, ozet.type]);
      const eski = b.anahtarlar.get(key);
      for (const alan of ['text', 'summary', 'data']) {
        if (ozet[alan] != null && typeof ozet[alan] !== 'string') { b.invalid = true; return; }
        b.bytes += new TextEncoder().encode(ozet[alan] ?? '').byteLength;
      }
      if (b.bytes > 4 * 1024 * 1024) { b.invalid = true; return; }
      if (!eski) {
        const row = structuredClone(ozet);
        b.details.push(row);
        b.anahtarlar.set(key, row);
        continue;
      }
      if (['id', 'format'].some((field) => eski[field] != null && ozet[field] != null && eski[field] !== ozet[field])) {
        b.invalid = true; return;
      }
      for (const alan of ['text', 'summary', 'data']) {
        if (typeof ozet?.[alan] === 'string') eski[alan] = (eski[alan] ?? '') + ozet[alan];
      }
    }
    for (const arac of d.tool_calls ?? []) if (typeof arac?.id === 'string') b.araclar.add(arac.id);
  }
}

export function sakla(oturum, b, model = '') {
  if (!oturum || durum(b) !== 'hazir' || !b.araclar.size) return false;
  const identity = JSON.stringify([oturum, model]);
  const kayit = DEPO.get(identity) ?? new Map();
  kayit.set(JSON.stringify([...b.araclar].sort()), structuredClone(b.details));
  while (kayit.size > TAVAN) kayit.delete(kayit.keys().next().value);
  DEPO.delete(identity);
  DEPO.set(identity, kayit);
  while (DEPO.size > TAVAN) DEPO.delete(DEPO.keys().next().value);
  return true;
}

// Returns: true if the body changed.
export function ekle(oturum, body, model = '') {
  const kayit = DEPO.get(JSON.stringify([oturum, model]));
  if (!kayit || !Array.isArray(body?.messages)) return false;
  let degisti = false;
  for (const mesaj of body.messages) {
    if (mesaj?.role !== 'assistant' || !Array.isArray(mesaj.tool_calls)
        || (mesaj.reasoning_details != null && (!Array.isArray(mesaj.reasoning_details) || mesaj.reasoning_details.length))) continue;
    const details = kayit.get(JSON.stringify(mesaj.tool_calls.map((t) => t?.id).filter(Boolean).sort()));
    if (details) { mesaj.reasoning_details = structuredClone(details); degisti = true; }
  }
  return degisti;
}
