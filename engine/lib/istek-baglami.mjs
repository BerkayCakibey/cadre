// B139/4c-4: the request context given by chat.headers stays valid for the SDK's retries with the same header; the record is not
// single-use, it is time-limited and capped (old records drop out, a close retry stays at the gate).
export class IstekBaglami {
  constructor(tavan = 2048, omurMs = 30 * 60 * 1000) {
    this.tavan = tavan;
    this.omurMs = omurMs;
    this.kayitlar = new Map();
  }

  koy(belirtec, kayit) {
    this.kayitlar.set(belirtec, { kayit, an: Date.now() });
    while (this.kayitlar.size > this.tavan) this.kayitlar.delete(this.kayitlar.keys().next().value);
  }

  al(belirtec) {
    const deger = this.kayitlar.get(belirtec);
    if (!deger) return undefined;
    if (Date.now() - deger.an > this.omurMs) { this.kayitlar.delete(belirtec); return undefined; }
    return deger.kayit;
  }

  get size() { return this.kayitlar.size; }
}
