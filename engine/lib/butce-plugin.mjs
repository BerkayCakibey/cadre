// OpenCode provider fetch boundary. No SDK dependency and no sidecar server.
// Native LLM transport must be disabled by the launcher: it bypasses SDK fetch.
import { execFileSync, execFile } from 'node:child_process';
import { mkdirSync, writeFileSync, unlinkSync, readFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { rolCiktisi, istenenCikti, SINIRLAR } from './sinirlar.mjs';   // 4c-4: output caps from one file (plan 64k, others 32k)
import { IstekBaglami } from './istek-baglami.mjs';
import { dusunmeAyarla, gonderilenEfor } from './dusunme.mjs';
import * as tasi from './dusunme-tasi.mjs';

const supported = new Set(['opencode-go', 'opencode', 'openrouter']);  // 0c: 'opencode' = zen; 4c-11: no free provider path
// B124: OBSERVATION MODE. In manual `opencode` use the gate does NOT BLOCK, it only observes — spending is written to the ledger,
// the call is not denied. In a run environment (CADRE_RUN_ID set) the mode is IGNORED: automated work is always subject to the cap.
const gozlemKipi = () => process.env.CADRE_BUTCE_GOZLEM === '1' && !process.env.CADRE_RUN_ID;
const butceReddi = (hata) => String(hata?.message ?? '').startsWith('CADRE_BUDGET_DENIED');
// G-B5 (B154): the denial receipt CARRIES the provider; if not, the engine falls back to the cautious default (all cloud closed).
const deny = (provider = null, reason = 'plugin refusal') => {
  try {
    execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/butce_kapisi.py')], {
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
    return JSON.parse(execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/butce_kapisi.py')], {
      input: JSON.stringify(request), encoding: 'utf8', timeout: 45000, maxBuffer: 65536,
      stdio: ['pipe', 'pipe', 'pipe'],
    }));
  } catch { return deny(request?.provider ?? null, 'ledger refused or unavailable'); }   // B169: the denial is recorded with the provider
}

function etkinCikti(model, agent) {
  const modelSiniri = number(model?.limit?.output);
  if (!Number.isSafeInteger(modelSiniri) || modelSiniri <= 0) return Number.NaN;
  return Math.min(rolCiktisi(process.env.CADRE_ROL ?? '', agent), modelSiniri);
}

// Item 0b: the bucket unit is the MODEL — a zero-priced model counts REQUESTS from the '<provider>:free' bucket.
function zeroCost(model) {
  const cost = model?.cost;
  if (!cost || typeof cost.input !== 'number' || typeof cost.output !== 'number') return false;
  return cost.input === 0 && cost.output === 0;
}

// OR-PIN: the reservation uses the MOST EXPENSIVE input/output rate of the pinned providers (the catalogue carries the cheapest);
// the price is written to zincir/openrouter-fiyat.json by butce.py `or_fiyat()`. If the file is missing/incomplete the reservation
// is DENIED (fail-closed): an unknown expensive route never produces a silent overrun. 4c-3/7: an endpoint that does not support the body's token parameter or
// cannot carry the requested output is dropped; order = this request's tokens x live price, endpoints with a low uptime ratio go last.
function orUcIzinli(kural, m, uc) {
  if (Array.isArray(kural)) return kural.includes(uc);
  if (!kural || typeof kural !== 'object') return false;
  const liste = kural.kademeler ?? kural.saglayicilar;
  if (Array.isArray(liste)) return liste.includes(uc);
  return kural.kapali === true || m?.kademeler?.[uc]?.kuantizasyon === (kural.kuantizasyon ?? 'fp8');
}

function orEnPahali(model, body = null, oturum = null) {
  const ad = String(model?.api?.id ?? model?.id ?? '').replace(/^openrouter\//, '');
  if (!ad) return null;
  try {
    const d = JSON.parse(readFileSync(join(process.env.CADRE_HOME ?? '', 'zincir', 'openrouter-fiyat.json'), 'utf8'));
    const m = (d?.modeller ?? {})[ad];
    const simdi = Date.now() / 1000, age = simdi - d.ts, fiyatYasi = simdi - (m?.fiyat_ts ?? d.ts);
    const sinir = SINIRLAR.openrouter;
    if (d.version !== 2 || !Number.isFinite(age) || age < 0 || age >= sinir.fiyat_taze_sn
        || !Number.isFinite(fiyatYasi) || fiyatYasi < 0 || fiyatYasi >= sinir.fiyat_yas_sn) return null;
    const kural = JSON.parse(readFileSync(new URL('./openrouter-politika.json', import.meta.url), 'utf8'))[ad];
    const nesne = !!kural && !Array.isArray(kural) && typeof kural === 'object';
    if (!kural || !Array.isArray(m?.providers) || !m.providers.length ||
        m.fp8_cache !== true || m.providers.some((uc) => !orUcIzinli(kural, m, uc))) return null;
    let providers = m.providers;
    let girdi = number(m?.input), cikti = number(m?.output);
    const secim = { neden: [], elenen: [] };
    let girdiTok = null;
    if (body) {
      let durum = {};
      try {
        durum = JSON.parse(readFileSync(process.env.CADRE_YON_DURUM ?? join(process.env.CADRE_HOME ?? '', 'zincir', 'saglayici-durum.json'), 'utf8'));
      } catch (error) { if (error.code !== 'ENOENT') return null; }
      const soguma = durum?.[`openrouter/${ad}`]?.uclar ?? {};
      const acik = providers.filter((p) => Number(soguma[p]?.cooldown_bitis ?? 0) <= simdi);
      // If no endpoint is open, keep the selection: the native reservation gate gives the recorded wait reason, no network is opened.
      if (acik.length) {
        for (const p of providers) if (!acik.includes(p)) secim.elenen.push(`${p}: endpoint cooldown`);
        providers = acik;
      }
    }
    if (body && m.kademeler) {
      const param = body.max_completion_tokens !== undefined ? 'max_completion_tokens' : 'max_tokens';
      let istenen = Number(body.max_completion_tokens ?? body.max_tokens ?? 0);
      const uyan = providers.filter((p) => !Array.isArray(m.kademeler[p]?.params) || m.kademeler[p].params.includes(param));
      const tasir = (p, n) => !(Number.isSafeInteger(m.kademeler[p]?.max_cikti) && n > m.kademeler[p].max_cikti);
      providers = uyan.filter((p) => tasir(p, istenen));
      for (const p of m.providers) {
        if (secim.elenen.includes(`${p}: endpoint cooldown`)) continue;
        if (!uyan.includes(p)) secim.elenen.push(`${p}: ${param} desteklenmiyor`);
        else if (!providers.includes(p)) secim.elenen.push(`${p}: cikti siniri ${m.kademeler[p]?.max_cikti}`);
      }
      const tavanlar = uyan.map((p) => m.kademeler[p]?.max_cikti).filter(Number.isSafeInteger);
      if (!providers.length && tavanlar.length) {
        // 4c QA2: if 2× the request fits no endpoint after truncation, no endpoint is dropped; the request is clipped to the largest endpoint cap
        istenen = Math.max(...tavanlar);
        body[param] = istenen;
        providers = uyan.filter((p) => tasir(p, istenen));
        secim.elenen = secim.elenen.filter((satir) => !providers.some((p) => satir.startsWith(`${p}: cikti`)));
      }
      if (!providers.length) return null;
      girdiTok = JSON.stringify(body).length + 4096;   // same rough estimate as requestQuote
      const maliyet = (p) => number(m.kademeler[p]?.input ?? m.input) * girdiTok + number(m.kademeler[p]?.output ?? m.output) * istenen;
      const dusuk = (p) => ((m.kademeler[p]?.uptime ?? 100) < sinir.dusuk_uptime ? 1 : 0);
      providers = providers.map((p, i) => [p, i])
        .sort((a, b) => dusuk(a[0]) - dusuk(b[0]) || maliyet(a[0]) - maliyet(b[0]) || a[1] - b[1]).map(([p]) => p);
      if (oturum) {
        const ipucu = ipucuOku();
        const son = String(process.env.CADRE_SURE_SON_TS ?? '').trim();
        const sonuc = oturumSirasi({ m, providers, girdi: girdiTok, istenen, simdi, devam: oturum.devam,
          kayit: ipucu.oturumlar[JSON.stringify([oturum.oturum, ad])] ?? null,
          uclar: Object.fromEntries(providers.map((p) => [p, ipucu.uclar[JSON.stringify([ad, p])]]).filter(([, v]) => v)),
          kalanSn: son && Number.isFinite(Number(son)) ? Number(son) - simdi : Number.NaN,
          yapiskanSn: Number(sinir.oturum_yapiskan_sn ?? 0), hataSn: Number(sinir.uc_hata_sn ?? 0), dusukUptime: sinir.dusuk_uptime });
        providers = sonuc.providers;
        secim.neden.push(...sonuc.neden);
        if (sonuc.oturum) secim.oturum = sonuc.oturum;
      }
      girdi = Math.max(...providers.map((p) => number(m.kademeler[p]?.input ?? m.input)));
      cikti = Math.max(...providers.map((p) => number(m.kademeler[p]?.output ?? m.output)));
    }
    if (!Number.isFinite(girdi) || !Number.isFinite(cikti) || girdi <= 0 || cikti <= 0) return null;
    const adlar = {};
    for (const p of providers) {
      const gorunen = m.kademeler?.[p]?.ad;
      if (typeof gorunen === 'string' && gorunen) adlar[gorunen] = gorunen in adlar ? null : p;
    }
    return { input: girdi, output: cikti, providers, etiketli: nesne && Array.isArray(kural.kademeler),
             kapali: nesne ? kural.kapali === true : ad.startsWith('openai/'), maxPrice: nesne ? (kural.max_price ?? null) : null,
             secim, girdiTok, adlar, fiyatTs: Number(m?.fiyat_ts ?? d.ts) };
  } catch { return null; }
}

export function oturumSirasi({ m, providers, girdi, istenen, kayit, uclar, kalanSn, simdi, yapiskanSn, hataSn, devam = null, dusukUptime = null }) {
  const neden = [];
  if (!(yapiskanSn > 0) && !(hataSn > 0)) return { providers: [...providers], neden };
  const oran = (p, alan) => number(m.kademeler?.[p]?.[alan] ?? m[alan]);
  const uptime = (p) => m.kademeler?.[p]?.uptime ?? 100;
  const saglik = (p) => (Number.isFinite(dusukUptime) && uptime(p) < dusukUptime ? 1 : 0);
  const okuma = (p) => {
    const r = m.kademeler?.[p]?.onbellek;
    return typeof r === 'number' && Number.isFinite(r) && r >= 0 && r < oran(p, 'input') ? r : oran(p, 'input');
  };
  const grup = (p) => {
    const u = uclar?.[p];
    if (hataSn > 0 && u?.son === 'hata' && simdi - Number(u.son_ts) < hataSn) return 2;
    if (Number.isFinite(kalanSn) && typeof u?.hiz_tps === 'number' && u.hiz_tps > 0 && istenen / u.hiz_tps > kalanSn) return 1;
    return 0;
  };
  const gruplar = new Map(providers.map((p) => [p, grup(p)]));
  for (const [p, g] of gruplar) if (g) neden.push(`${p}: ${g === 2 ? 'son sonuc hata' : 'olculen hizla kalan sureye sigmaz'}`);
  let sira = providers.map((p, i) => [p, i]).sort((a, b) => gruplar.get(a[0]) - gruplar.get(b[0]) || a[1] - b[1]).map(([p]) => p);
  const yas = simdi - Number(kayit?.ts);
  const taze = yapiskanSn > 0 && kayit && yas >= 0 && yas <= yapiskanSn;
  const oturumUcu = taze ? (kayit.uc ?? kayit.sira) : null;
  let oturum;
  if (taze && kayit.basari === false) {
    neden.push('oturum ucu son istekte basarisiz: yapiskanlik yok');
  } else if (oturumUcu && sira.includes(oturumUcu)) {
    const ayniSinif = (p) => p !== oturumUcu && gruplar.get(p) === 0 && saglik(p) === saglik(oturumUcu);
    const saglikli = sira.find((p) => p !== oturumUcu && gruplar.get(p) === 0 && saglik(p) < saglik(oturumUcu));
    const alternatif = sira.find(ayniSinif);
    if (gruplar.get(oturumUcu) !== 0) {
      neden.push(`oturum ucu ${oturumUcu} korunmadi: ${gruplar.get(oturumUcu) === 2 ? 'hata' : 'sigmaz'}`);
    } else if (saglikli) {
      neden.push(`oturum ucu ${oturumUcu} korunmadi: uptime ${uptime(oturumUcu)} < ${dusukUptime} (${saglikli} saglikli)`);
    } else if (!alternatif) {
      sira = [oturumUcu, ...sira.filter((p) => p !== oturumUcu)];
      neden.push(`oturum ucu ${oturumUcu} korundu: uygun alternatif yok`);
    } else {
      const gercekUc = typeof kayit.uc === 'string' && kayit.uc === oturumUcu;
      const onceki = devam === true && gercekUc && Number.isSafeInteger(kayit.karakter) && kayit.karakter > 0 ? kayit.karakter : 0;
      if (devam === false) neden.push('onek kirik: onbellek kazanci yok');
      else if (devam !== true) neden.push('onek dogrulanmadi: onbellek kazanci yok');
      else if (!gercekUc) neden.push('gercek uc bilinmiyor: onbellek kazanci yok');
      const okunan = Math.min(girdi, onceki);
      oturum = { uc: oturumUcu, uc_kaynagi: gercekUc ? 'yanit' : 'ilk_tercih', onek: devam === true || devam === false ? devam : null,
                 onbellek_kredisi: okunan };
      const kalis = (girdi - okunan) * oran(oturumUcu, 'input') + okunan * okuma(oturumUcu) + istenen * oran(oturumUcu, 'output');
      const gecis = girdi * oran(alternatif, 'input') + istenen * oran(alternatif, 'output');
      if (kalis <= gecis) {
        sira = [oturumUcu, ...sira.filter((p) => p !== oturumUcu)];
        neden.push(`oturum ucu ${oturumUcu} korundu: kalis ${Math.round(kalis)} <= gecis ${Math.round(gecis)} (${alternatif})`);
      } else {
        neden.push(`oturum ucu ${oturumUcu} birakildi: gecis ${Math.round(gecis)} (${alternatif}) < kalis ${Math.round(kalis)}`);
      }
    }
  }
  return { providers: sira, neden, ...(oturum ? { oturum } : {}) };
}

const ipucuYolu = () => join(process.env.CADRE_HOME ?? '', 'zincir', 'or-oturum-uclari.json');

function ipucuOku() {
  try {
    const d = JSON.parse(readFileSync(ipucuYolu(), 'utf8'));
    if (d?.surum === 1 && d.oturumlar && typeof d.oturumlar === 'object' && d.uclar && typeof d.uclar === 'object') return d;
  } catch {}
  return { surum: 1, oturumlar: {}, uclar: {} };
}

function ipucuYaz(degistir) {
  try {
    const d = ipucuOku();
    degistir(d);
    const simdi = Date.now() / 1000;
    const taze = (kayitlar, tavan) => Object.fromEntries(Object.entries(kayitlar)
      .filter(([, v]) => typeof v?.ts === 'number' && simdi - v.ts < 86400)
      .sort((a, b) => b[1].ts - a[1].ts).slice(0, tavan));
    d.oturumlar = taze(d.oturumlar, 256);
    d.uclar = taze(d.uclar, 128);
    mkdirSync(join(process.env.CADRE_HOME ?? '', 'zincir'), { recursive: true });
    const gecici = `${ipucuYolu()}.${process.pid}.${randomUUID().slice(0, 8)}`;
    writeFileSync(gecici, JSON.stringify(d));
    renameSync(gecici, ipucuYolu());
  } catch {}
}

function oturumSonucu(bag, { basari, ucAdi = null, cikti = null, sureMs = null }) {
  if (!bag?.oturum) return;
  const etiket = (ucAdi && bag.adlar[ucAdi]) || bag.tekUc || null;
  ipucuYaz((d) => {
    const simdi = Date.now() / 1000;
    d.oturumlar[JSON.stringify([bag.oturum, bag.ad])] = { sira: bag.sira, uc: etiket, basari, karakter: bag.karakter, ts: simdi };
    if (!etiket) return;
    const anahtar = JSON.stringify([bag.ad, etiket]);
    const uc = { basari: 0, hata: 0, ...(d.uclar[anahtar] ?? {}) };
    uc[basari ? 'basari' : 'hata'] += 1;
    Object.assign(uc, { son: basari ? 'basari' : 'hata', son_ts: simdi, ts: simdi });
    if (basari && Number.isSafeInteger(cikti) && cikti >= 32 && sureMs >= 500) uc.hiz_tps = Math.round(cikti / (sureMs / 1000) * 100) / 100;
    d.uclar[anahtar] = uc;
  });
}

const ONEKLER = new Map();
const ozetle = (deger) => createHash('sha256').update(JSON.stringify(deger ?? null)).digest('hex');

function onekOlc(oturum, body) {
  const liste = [body?.messages, body?.input, body?.contents].find(Array.isArray) ?? [];
  const sistem = liste.filter((m) => m?.role === 'system' || m?.role === 'developer');
  const sabit = ozetle([body?.system ?? body?.instructions ?? body?.systemInstruction ?? null, body?.tools ?? null, sistem]).slice(0, 16);
  const tum = ozetle(liste);
  const onceki = oturum ? ONEKLER.get(oturum) : undefined;
  const devam = onceki ? onceki.sabit === sabit && onceki.n <= liste.length && ozetle(liste.slice(0, onceki.n)) === onceki.tum : null;
  if (oturum) {
    ONEKLER.delete(oturum);
    ONEKLER.set(oturum, { sabit, n: liste.length, tum });
    while (ONEKLER.size > 512) ONEKLER.delete(ONEKLER.keys().next().value);
  }
  return { sabit, devam, mesaj: liste.length };
}

function efor(body, provider, url) {
  const seviye = metin(gonderilenEfor(body, provider, url), 16);
  const politika = metin(process.env.CADRE_EFOR_POLITIKASI, 32);
  return seviye || politika ? { efor: { seviye: seviye ?? null, politika: politika ?? null } } : {};
}

const tamSayi = (v) => (Number.isSafeInteger(v) && v >= 0 ? v : undefined);
const tutar = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : undefined);
const metin = (v, n) => (typeof v === 'string' && v.length > 0 && v.length <= n && /^[A-Za-z0-9 ._:/()+@-]+$/.test(v) ? v : undefined);

function kullanimOzeti(u, bicim) {
  if (!u || typeof u !== 'object') return {};
  const okuma = tamSayi(u.prompt_tokens_details?.cached_tokens ?? u.input_tokens_details?.cached_tokens
                        ?? u.cache_read_input_tokens ?? u.cachedContentTokenCount);
  const yazma = tamSayi(u.prompt_tokens_details?.cache_write_tokens ?? u.cache_creation_input_tokens
                        ?? u.prompt_tokens_details?.cache_creation_tokens);
  let girdi = tamSayi(u.prompt_tokens ?? u.promptTokenCount ?? u.input_tokens);
  if (bicim === 'anthropic' && girdi !== undefined) girdi += (okuma ?? 0) + (yazma ?? 0);
  const dusunme = tamSayi(u.completion_tokens_details?.reasoning_tokens ?? u.output_tokens_details?.reasoning_tokens ?? u.thoughtsTokenCount);
  let cikti = tamSayi(u.completion_tokens ?? u.output_tokens ?? u.candidatesTokenCount);
  if (bicim === 'usage-metadata' && cikti !== undefined && dusunme !== undefined) cikti += dusunme;
  return { girdi, cikti, dusunme, onbellek_okuma: okuma, onbellek_yazma: yazma, ucret_usd: tutar(u.cost),
           onbellek_indirimi_usd: tutar(u.cache_discount), ust_ucret_usd: tutar(u.cost_details?.upstream_inference_cost),
           byok: typeof u.is_byok === 'boolean' ? u.is_byok : undefined };
}

function quote(model, maximum) {
  const sag = model?.providerID ?? null;
  const input = number(model.limit?.input ?? model.limit?.context);
  const output = Math.min(number(model.limit?.output), number(maximum));
  if (!Number.isSafeInteger(input) || !Number.isSafeInteger(output) || input <= 0 || output <= 0) return deny(sag, 'pricing unknown');
  // OR-PIN: the OpenRouter reservation uses the most expensive pinned provider rate; no cache discount is assumed
  // (Relace charged cached input at FULL price). The receipt closes with the real usage.cost in the response.
  if (sag === 'openrouter') {
    const enPahali = orEnPahali(model);
    if (!enPahali) return deny(sag, 'openrouter fail-closed pricing unavailable');
    return { input, output, inRate: enPahali.input, freshRate: enPahali.input, readRate: enPahali.input,
             writeRate: enPahali.input, outRate: enPahali.output,
             maximum: Math.ceil(input * enPahali.input + output * enPahali.output) };
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
  if (!Number.isFinite(inRate) || !Number.isFinite(outRate) || inRate <= 0 || outRate <= 0) return deny(sag, 'pricing unknown');
  return { input, output, inRate, freshRate, readRate, writeRate, outRate,
           maximum: Math.ceil(input * inRate + output * outRate) };
}

  // The reservation is priced by the size of the REQUEST, not by the model's full context (B36).
function requestQuote(pricing, rawBody, cikti = pricing.output) {
  const chars = number(rawBody.length);
  const inputTokens = Math.min(pricing.input, chars + 4096);
  // With time-dependent pricing the reservation ALWAYS assumes peak (2x): off-peak, the excess comes back with the receipt,
  // at peak the budget is not exceeded. If an error must be made in an unknown direction, it is made in the expensive direction.
  // Output is the request's total cap (Anthropic thinking budget included): the worst case is priced.
  const zaman = pricing.zamanli ? 2 : 1;
  return Math.ceil(zaman * (inputTokens * pricing.inRate + Math.max(pricing.output, cikti) * pricing.outRate));
}

// 4c-13: Go charges 2× points for DeepSeek models at peak hours (01-04, 06-10 UTC, Mon-Fri) (opencode.ai/docs/go); the catalogue price
// is off-peak. The reservation always uses the peak price (fail-closed), the receipt closes with the multiplier at that time.
const zamanli = (provider, model) => provider === 'opencode-go' && String(model ?? '').startsWith('deepseek');
function zamanCarpani() {
  const now = new Date();
  const gun = now.getUTCDay();
  if (gun === 0 || gun === 6) return 1;
  const saat = now.getUTCHours();
  return ((saat >= 1 && saat < 4) || (saat >= 6 && saat < 10)) ? 2 : 1;
}

const tamsayi = (value, varsayilan = 0) => (Number.isSafeInteger(value) && value >= 0 ? value : varsayilan);

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
  const okunan = tamsayi(usage.prompt_tokens_details?.cached_tokens ?? usage.input_tokens_details?.cached_tokens
                         ?? usage.cache_read_input_tokens ?? usage.cachedContentTokenCount);
  const yazilan = tamsayi(usage.cache_creation_input_tokens ?? usage.prompt_tokens_details?.cache_creation_tokens);
  const taze = Math.max(0, input - Math.min(input, okunan + yazilan));
  const okunanSayi = Math.min(input, okunan);
  const yazilanSayi = Math.min(Math.max(0, input - okunanSayi), yazilan);
  const freshRate = pricing.freshRate ?? pricing.inRate;
  const readRate = pricing.readRate ?? freshRate;
  const writeRate = pricing.writeRate || freshRate;
  const carpan = pricing.zamanli ? zamanCarpani() : 1;
  return Math.ceil(carpan * (taze * freshRate + okunanSayi * readRate + yazilanSayi * writeRate
                             + (output + reasoning) * pricing.outRate));
}

// B310: in the Anthropic Messages stream (Go qwen3.8-flash, @ai-sdk/anthropic) input_tokens does NOT INCLUDE the cache counts;
// usage is split across message_start + message_delta. A missing or invalid number returns null (no receipt, the reservation stays).
function anthropicCost(u, pricing) {
  const taze = u.input_tokens, okunan = u.cache_read_input_tokens ?? 0, yazilan = u.cache_creation_input_tokens ?? 0, cikti = u.output_tokens;
  if (![taze, okunan, yazilan, cikti].every(v => Number.isSafeInteger(v) && v >= 0)) return null;
  const freshRate = pricing.freshRate ?? pricing.inRate;
  const carpan = pricing.zamanli ? zamanCarpani() : 1;
  return Math.ceil(carpan * (taze * freshRate + okunan * (pricing.readRate ?? freshRate)
                             + yazilan * (pricing.writeRate || freshRate) + cikti * pricing.outRate));
}

// Receipt of the free bucket: what is measured is the REQUEST COUNT, not dollars, so 1 is written whatever the outcome.
function bedavaSay(reservation, receivedID) {
  try {
    ledger({ action: 'settle', id: reservation.id, actual: 1,
      receipt: `${reservation.id}:${receivedID || 'free-request'}` });
  } catch { /* if the count cannot be written the reservation stays; no money risk */ }
}

// CANLI1/4c-4: the open-request marker carries the time and type of the last chunk. Progress = a token or usage chunk; an SSE comment
// (OR ': OPENROUTER PROCESSING') only shows the connection is open. The guard (canlilik.py) reads progress; no fixed timeout.
const BOS_PARCA = /"type"\s*:\s*"ping"|"delta"\s*:\s*\{\s*(?:"role"\s*:\s*"assistant"\s*,?\s*)?(?:"content"\s*:\s*(?:""|null)\s*)?\}/;

function parcaTuru(metin) {
  let tur = '';
  for (const satir of metin.split('\n')) {
    const veri = satir.startsWith('data:') ? satir.slice(5).trim() : '';
    if (veri && veri !== '[DONE]') {
      if (/"usage"\s*:\s*\{/.test(veri)) return 'usage';
      if (!BOS_PARCA.test(veri)) tur = 'token';
      else if (!tur) tur = 'yorum';
    } else if (satir.startsWith(':') && !tur) tur = 'yorum';
  }
  return tur;
}

async function canliFetch(fetcher, url, options, local = false) {
  const abort = new AbortController();
  const relay = () => abort.abort(options.signal.reason);
  if (options.signal?.aborted) relay();
  else options.signal?.addEventListener('abort', relay, { once: true });
  let marker, isaret, sonYazma = 0;
  const decoder = new TextDecoder();
  const yaz = (zorla = false) => {
    if (!marker || (!zorla && Date.now() - sonYazma < 1000)) return;   // written at most once per second
    sonYazma = Date.now();
    try { writeFileSync(marker, JSON.stringify(isaret)); } catch {}
  };
  if (process.env.CADRE_REQUEST_DIR) {
    mkdirSync(process.env.CADRE_REQUEST_DIR, { recursive: true });
    marker = join(process.env.CADRE_REQUEST_DIR, `${randomUUID()}.json`);
    isaret = { pid: process.pid, started: Date.now(), local, last_chunk_at: null, ilerleme_at: null, tur: '' };
    yaz(true);
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
        if (isaret) {
          const tur = sse ? parcaTuru(decoder.decode(chunk.value, { stream: true })) : 'token';
          const ilerleme = tur === 'token' || tur === 'usage';
          const ilk = ilerleme && isaret.ilerleme_at === null;
          isaret.last_chunk_at = Date.now();
          if (tur) isaret.tur = tur;
          if (ilerleme) isaret.ilerleme_at = isaret.last_chunk_at;
          yaz(ilk);
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

function uretimKaydet(id, generation) {
  try {
    execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/butce_kapisi.py')], {
      input: JSON.stringify({ action: 'gen', id, generation }), timeout: 10000, stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch {}
}

function saglayiciHatasi(reservation, hata, kod) {
  if (!hata || typeof hata !== 'object') return;
  try {
    execFileSync(process.env.CADRE_PYTHON, [join(process.env.CADRE_ENGINE_ROOT, 'lib/butce_kapisi.py')], {
      input: JSON.stringify({ action: 'saglayici-hatasi', id: reservation.id, kod: Number.isInteger(hata.code) ? hata.code : kod,
        tur: metin(hata.metadata?.error_type, 64) ?? '', mesaj: metin(hata.message, 160) ?? '' }),
      timeout: 10000, stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch {}
}

async function receiptStream(response, reservation, pricing, isFree, provider = null, oturum = null, model = '', ek = {}) {
  // G-B2/P5: a free request that was sent counts even if the body fails later.
  if (isFree) bedavaSay(reservation, '');
  if (!isFree && response.status === 429) {
    // 4c-4: 429 says the request was not processed; the SDK retries with the same header, no double obligation is left (the receipt stays recorded)
    try { ledger({ action: 'settle', id: reservation.id, actual: 0, receipt: `${reservation.id}:http-429` }); } catch {}
    oturumSonucu(ek.orBag, { basari: false });
    return response;
  }
  if (!response.ok || !response.body) {
    if (!response.ok) oturumSonucu(ek.orBag, { basari: false });
    if (!isFree && response.status >= 500) {
      let govde = null;
      try { govde = JSON.parse(await response.clone().text()); } catch {}
      saglayiciHatasi(reservation, govde?.error ?? { code: response.status }, response.status);
    }
    return response;                                    // Other denials may carry an unknown amount: the reservation stays.
  }
  const sse = (response.headers.get('content-type') ?? '').includes('text/event-stream');
  const decoder = new TextDecoder();
  let buffer = '', used = null, finished = false, invalid = false, receivedID = '', anth = null, reasoningComplete = false;
  let uretimYazildi = false, hataYazildi = false;
  let sonKullanim = null, indirim, ucAdi, modelYanit, bitis;
  const ozetler = provider === 'openrouter' ? tasi.birikim() : null;
  const item = (raw) => {
    if (raw === '[DONE]') { finished = true; return; }
    try {
      const value = JSON.parse(raw);
      if (ozetler) tasi.parca(ozetler, value);
      if (typeof value?.provider === 'string') ucAdi = value.provider;
      modelYanit = metin(value?.model ?? value?.message?.model ?? value?.response?.model ?? value?.modelVersion, 128) ?? modelYanit;
      indirim = tutar(value?.cache_discount) ?? indirim;
      if (value.type === 'message_start' || value.type === 'message_delta' || value.type === 'message_stop') {
        if (typeof value.message?.id === 'string') receivedID = value.message.id;
        anth = { ...anth, ...(value.message?.usage ?? {}), ...(value.usage ?? {}) };
        sonKullanim = { u: anth, bicim: 'anthropic' };
        bitis = value.delta?.stop_reason ?? bitis;
        if (!isFree) used = provider === 'openrouter' ? usageCost({ usage: anth }, pricing, provider) : anthropicCost(anth, pricing);
        if (value.type === 'message_stop' || (value.type === 'message_delta' && value.delta?.stop_reason != null)) finished = true;
        return;
      }
      if (typeof value.id === 'string') receivedID = value.id;
      else if (typeof value.response?.id === 'string') receivedID = value.response.id;
      if (provider === 'openrouter' && !isFree && !uretimYazildi && /^gen-[A-Za-z0-9_-]{1,128}$/.test(receivedID)) {
        uretimYazildi = true;
        uretimKaydet(reservation.id, receivedID);
      }
      if (!isFree && !hataYazildi && value?.error && typeof value.error === 'object') {
        hataYazildi = true;
        saglayiciHatasi(reservation, value.error, null);
      }
      if (value.choices?.some(c => c.finish_reason != null)) {
        reasoningComplete = value.choices.every(c => ['tool_calls', 'function_call', 'stop'].includes(c.finish_reason));
        bitis = value.choices.find(c => c.finish_reason != null).finish_reason;
      }
      bitis = value.candidates?.find(c => c.finishReason)?.finishReason ?? value.response?.status ?? bitis;
      const kullanim = value.usage ?? value.usageMetadata ?? value.response?.usage;
      if (kullanim && typeof kullanim === 'object') {
        sonKullanim = { u: kullanim, bicim: value.usageMetadata ? 'usage-metadata' : value.response?.usage ? 'responses' : 'chat' };
      }
      if (!isFree) used = usageCost(value, pricing, provider) ?? used;
      if (value.choices?.some(c => c.finish_reason != null) || value.candidates?.some(c => c.finishReason)
          || value.type === 'response.completed' || value.type === 'response.incomplete') finished = true;
    } catch { invalid = true; }
  };
  const makbuzKullanimi = (sureMs) => Object.fromEntries(Object.entries({
    ...kullanimOzeti(sonKullanim?.u, sonKullanim?.bicim), ...(indirim !== undefined ? { onbellek_indirimi_usd: indirim } : {}),
    uc: metin(ucAdi, 64), uc_etiket: ucAdi ? ek.orBag?.adlar?.[ucAdi] ?? undefined : undefined, model_yanit: modelYanit,
    bitis: metin(bitis, 32), bicim: sonKullanim?.bicim, fiyat_kaynagi: ek.fiyatKaynagi, fiyat_ts: ek.fiyatTs, sure_ms: sureMs,
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
      if (ozetler && finished && !invalid && reasoningComplete) tasi.sakla(oturum, ozetler, model);
      const sureMs = Number.isFinite(ek.basla) ? Math.max(0, Math.round(Date.now() - ek.basla)) : undefined;
      const kullanim = makbuzKullanimi(sureMs);
      oturumSonucu(ek.orBag, { basari: finished && !invalid && bitis !== 'error', ucAdi, cikti: kullanim.cikti, sureMs });
      // What the free bucket measures is the REQUEST COUNT: even if the stream ends broken, the request was made and counts.
      if (isFree) return;
      if (invalid || !finished || used === null) return;
      // B313b reverted: a stuck request had its receipt written, so the synchronous settle did not lock the stream;
      // the asynchronous settle pushed the receipt past the stream close and left readers empty.
      // This transport closure is the request identity, including each retry.
      try {
        ledger({ action: 'settle', id: reservation.id,
          actual: Math.ceil(used),
          receipt: `${reservation.id}:${receivedID || 'complete-response'}`, kullanim });
      } catch { /* Keep the durable full reservation if receipt recording fails. */ }
    },
  });
  return new Response(response.body.pipeThrough(tap), { status: response.status, statusText: response.statusText, headers: response.headers });
}

export default async function CadreBudget() {
  // a second copy in the same process (cadre-plugin + butce-plugin, live + repo root) does not install the gate again; if it did, the header
  // token would not be found in the other copy and every call would be denied.
  const tekil = Symbol.for(`cadre.saglayici-kapisi:${process.env.CADRE_HOME ?? ''}`);
  const projectTools = process.env.CADRE_PROJECT_TOOLS === '1';
  let toolPolicy;
  if (projectTools) {
    try { toolPolicy = JSON.parse(process.env.CADRE_TOOL_POLICY ?? ''); } catch {}
    if (toolPolicy?.version !== 1 || toolPolicy.sandbox !== '/usr/bin/sandbox-exec'
        || typeof toolPolicy.profile !== 'string' || !toolPolicy.scratch?.startsWith('/')) {
      throw new Error('CADRE_TOOL_DENIED: project sandbox unavailable');
    }
    toolPolicy = Object.freeze(toolPolicy);
    if (toolPolicy.arac !== undefined) {
      let ozet = '';
      try { ozet = createHash('sha256').update(readFileSync(toolPolicy.arac)).digest('hex'); } catch {}
      if (typeof toolPolicy.arac !== 'string' || !toolPolicy.arac.startsWith('/') || ozet !== toolPolicy.arac_sha256) {
        throw new Error('CADRE_TOOL_DENIED: project sandbox wrapper untrusted');
      }
    }
  }
  if (globalThis[tekil]) {
    if (projectTools && globalThis[tekil]?.toolPolicy !== process.env.CADRE_TOOL_POLICY) {
      throw new Error('CADRE_TOOL_DENIED: existing provider has no matching project sandbox');
    }
    return {};
  }
  globalThis[tekil] = projectTools ? { toolPolicy: process.env.CADRE_TOOL_POLICY } : true;
  const shellQuote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
  const toolEnvironment = ['PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'PYTHONPATH', 'VIRTUAL_ENV',
    'CADRE_HOME', 'CADRE_ENGINE_ROOT', 'CADRE_RUN_ID', 'CADRE_GOREV_ID']
    .filter((key) => typeof process.env[key] === 'string')
    .map((key) => `${key}=${shellQuote(process.env[key])}`).join(' ');
  const models = new IstekBaglami();
  const wrappers = new Map();
  return {
    async 'tool.execute.before'(input, output) {
      if (process.env.CADRE_RUN_ID && /^(plan-review-[1-9][0-9]*-[a-f0-9]{8}|review-(author|final|recheck)-[a-f0-9]{12})$/.test(process.env.CADRE_GOREV_ID ?? '')) {
        const arac = ledger({ action: 'review-tool' });
        if (arac?.tool_limit) {
          throw new Error(`CADRE_TOOL_LIMIT: ${arac.tool_limit}; yeni arac cagrisi yapma, eldeki kanitla son cevabi ver`);
        }
      }
      if (!projectTools) return;
      if (input.tool === 'bash' && typeof output.args?.command === 'string') {
        let komut = output.args.command;
        const onek = toolPolicy.arac ? `exec ${shellQuote(toolPolicy.arac)} ` : null;
        while (onek && komut.startsWith(onek) && /^'(?:[^']|'\\'')*'$/.test(komut.slice(onek.length))) {
          komut = komut.slice(onek.length + 1, -1).replaceAll("'\\''", "'");
        }
        output.args.command = toolPolicy.arac
          ? `${onek}${shellQuote(komut)}`
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
        if (gozlemKipi()) return;
        return deny();
      }
      const providers = config.provider ?? {};
      const enabled = [];
      for (const provider of supported) {
        const settings = providers[provider] ?? {};
        const previous = settings.options?.fetch ?? globalThis.fetch;
        const kapili = async (url, options = {}) => {
          // Catalogue GETs are unbilled; unknown/multipart/inference transports
          // are not admitted. Redirects must not replay a POST outside the gate.
          if ((options.method ?? 'GET').toUpperCase() === 'GET') return previous(url, options);
          if (typeof options.body !== 'string') return deny(provider, 'unsupported request body');
          let body;
          try { body = JSON.parse(options.body); } catch { return deny(provider, 'request body not json'); }
          const pathModel = String(url).match(/\/models\/([^/:]+):/);
          const name = body.model ?? (pathModel ? decodeURIComponent(pathModel[1]) : null);
          const headers = new Headers(options.headers);
          const entry = models.al(headers.get('x-cadre-budget-context'));
          headers.delete('x-cadre-budget-context');
          const aylikKimlik = headers.get('x-cadre-aylik-kimlik');
          headers.delete('x-cadre-aylik-kimlik');
          if (!entry || entry.provider !== provider || entry.name !== name) return deny(provider, 'request context missing');
          if (dusunmeAyarla(body, { provider, model: name, rol: process.env.CADRE_ROL, ajan: entry.role, url })) {
            options = { ...options, body: JSON.stringify(body) };
          }
          const { toplam, gorunur } = istenenCikti(body);   // 16-4: with budget-type thinking, max_tokens − budget is measured
          if (!Number.isSafeInteger(gorunur) || gorunur <= 0 || gorunur > entry.pricing.output) return deny(provider, 'output cap invalid');
          // OR-PIN: ask for the real usage.cost in the response (so the ledger records reality, not the estimate).
          let orBag = null, ucSecimi = null, fiyatTs;
          if (provider === 'openrouter') tasi.ekle(entry.session, body, name);   // 4c-4: missing reasoning_details are put back on a tool round
          const onek = onekOlc(entry.session, body);
          if (provider === 'openrouter') {
            if (!entry.counted) {
              const prices = orEnPahali({ id: name }, body, { oturum: entry.session, devam: onek.devam });
              if (!prices) return deny(provider, 'openrouter fail-closed pricing unavailable');
              orBag = { oturum: entry.session, ad: name, sira: prices.providers[0], karakter: prices.girdiTok, adlar: prices.adlar,
                        tekUc: prices.providers.length === 1 ? prices.providers[0] : null };
              ucSecimi = { sira: prices.providers, neden: prices.secim.neden, ...(prices.secim.elenen.length ? { elenen: prices.secim.elenen } : {}),
                           ...(prices.secim.oturum ? { oturum: prices.secim.oturum } : {}) };
              fiyatTs = Number.isFinite(prices.fiyatTs) && prices.fiyatTs > 0 ? prices.fiyatTs : undefined;
              // 4b: comes from a closed-weight vendor, its endpoints do not report quantization; the fp8 filter would close it entirely.
              // 4c-3: in a tiered policy the parameter map replaces require_parameters, the maximum price goes in the request.
              body.provider = { only: prices.providers, order: prices.providers, ...(prices.kapali ? {} : { quantizations: ['fp8'] }),
                                allow_fallbacks: false, ...(prices.maxPrice ? { max_price: prices.maxPrice } : {}),
                                ...(prices.kapali ? {} : { require_parameters: true }) };   // closed weights (Sol/qwen): endpoint parameter map
              entry.pricing = { ...entry.pricing, inRate: prices.input, freshRate: prices.input,
                                readRate: prices.input, writeRate: prices.input, outRate: prices.output };
            } else if (!String(name).endsWith(':free')) {
              return deny(provider, 'openrouter free model identity required');
            }
            body.usage = { include: true };
            options = { ...options, body: JSON.stringify(body) };
          }
          const reservation = ledger({ action: 'reserve', provider: entry.bucket,
            maximum: entry.counted ? 1 : requestQuote(entry.pricing, options.body, toplam),
            metadata: { model: name, session_id: entry.session, role: entry.role, pricing: entry.pricing, onek,
                        ...efor(body, provider, url),
                        ...(ucSecimi ? { uc_secimi: ucSecimi } : {}), ...(aylikKimlik ? { aylik_id: aylikKimlik } : {}) } });
          // The reservation is written BEFORE the network. In a paid bucket, if the outcome is unknown the reservation STAYS (to avoid
          // a silent overrun); in a free bucket the request counts whatever the outcome (B83).
          if (process.env.CADRE_ISTEK_ORNEGI) {   // probe/canary: the body leaving the gate goes to a local file (headers and keys are not written)
            try {
              mkdirSync(process.env.CADRE_ISTEK_ORNEGI, { recursive: true });
              writeFileSync(join(process.env.CADRE_ISTEK_ORNEGI, `${Date.now()}-${provider}-${randomUUID().slice(0, 8)}.json`), options.body);
            } catch {}
          }
          let response;
          const basla = Date.now();
          try {
            response = await canliFetch(previous, url, { ...options, headers, redirect: 'error' });
          } catch (hata) {
            if (entry.counted) bedavaSay(reservation, '');
            oturumSonucu(orBag, { basari: false });
            throw hata;
          }
          // Item 5: 429 = window wall. The stamp file had NEVER BEEN WRITTEN until now (butce.py read it
          // but nobody wrote it), so the "we hit the limit" signal was dead code. It is now written at the limit.
          if (response.status === 429 && provider === 'opencode-go') {
            try {
              mkdirSync(join(process.env.CADRE_HOME, 'zincir'), { recursive: true });
              writeFileSync(join(process.env.CADRE_HOME, 'zincir', 'go-429-son.txt'), String(Math.floor(Date.now() / 1000)));
            } catch { /* if the stamp cannot be written the call is still denied; the percentage gate works separately */ }
          }
          return receiptStream(response, reservation, entry.pricing, entry.counted, provider, entry.session, name,   // B169: usage denial with provider
            { basla, orBag, fiyatTs, fiyatKaynagi: provider === 'openrouter' && !entry.counted ? 'openrouter-uclari' : 'opencode-katalog' });
        };
        // B124: in observation mode a budget denial does NOT BLOCK the call — the raw call passes, the record stays as attempted.
        const guarded = async (url, options = {}) => {
          try {
            return await kapili(url, options);
          } catch (hata) {
            if (provider !== 'openrouter' && gozlemKipi() && butceReddi(hata)) return previous(url, options);
            throw hata;
          }
        };
        providers[provider] = { ...settings, options: { ...settings.options, fetch: guarded } };
        wrappers.set(provider, guarded);
        enabled.push(provider);
      }
      // B116: thinking level per role (DUSUNME_ROL).
      // Hence the role split: MECHANICAL roles want speed -> 'none'; JUDGMENT roles want accuracy -> prefill.
      config.provider = providers;
      config.enabled_providers = enabled; // Last mutation, only after guard installation.
    },
    async 'chat.params'(input, output) {
      const provider = input.model.providerID;
      if (!wrappers.has(provider)) return deny(provider, 'provider not guarded');
      let pricing;
      // Plan is raised to the role cap; other roles clip what opencode asks for to the role cap (invalid request NaN -> denial, B186).
      const planning = input.agent === 'plan' || input.agent === 'cadre-plan' || process.env.CADRE_ROL === 'plan';
      let sinir = etkinCikti(input.model, input.agent);
      if (!Number.isSafeInteger(sinir)) return deny(provider, 'model output capacity unknown');
      if (!planning && process.env.CADRE_CIKTI_KAT !== '2' && output.maxOutputTokens !== undefined) sinir = Math.min(sinir, number(output.maxOutputTokens));
      output.maxOutputTokens = sinir;
      if (zeroCost(input.model)) {
        const cap = number(output.maxOutputTokens);
        // B186: number() returns NaN; an invalid limit is denied here, before the provider call.
        if (!Number.isSafeInteger(cap) || cap <= 0) return deny(provider, 'output cap invalid');
        pricing = { output: cap };
      } else {
        pricing = { ...quote(input.model, output.maxOutputTokens), zamanli: zamanli(provider, input.model.api?.id ?? input.model.id) };
      }
      output.maxOutputTokens = pricing.output;
    },
    async 'chat.headers'(input, output) {
      const provider = input.model.providerID;
      if (!wrappers.has(provider)) return deny(provider, 'provider not guarded');
      // counted = a bucket that counts REQUESTS instead of dollars: a zero-priced model (zen/Go free models).
      const counted = zeroCost(input.model);
      const modelAdi = input.model.api?.id ?? input.model.id;
      const bucket = counted ? `${provider}:free` : provider;
      const sinir = etkinCikti(input.model, input.agent);
      if (!Number.isSafeInteger(sinir)) return deny(provider, 'model output capacity unknown');
      const pricing = counted ? { output: sinir } : { ...quote(input.model, sinir), zamanli: zamanli(provider, modelAdi) };
      const token = randomUUID();
      models.koy(token, { provider, bucket, counted, name: modelAdi, pricing, session: input.sessionID, role: input.agent });
      output.headers['x-cadre-budget-context'] = token;
    },
  };
}
