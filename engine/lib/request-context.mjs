// B139/4c-4: the request context given by chat.headers stays valid for the SDK's retries with the same header; the record is not
// single-use, it is time-limited and capped (old records drop out, a close retry stays at the gate).
export class RequestContext {
  constructor(cap = 2048, lifetimeMs = 30 * 60 * 1000) {
    this.cap = cap;
    this.lifetimeMs = lifetimeMs;
    this.records = new Map();
  }

  put(token, record) {
    this.records.set(token, { record, createdAt: Date.now() });
    while (this.records.size > this.cap) this.records.delete(this.records.keys().next().value);
  }

  get(token) {
    const value = this.records.get(token);
    if (!value) return undefined;
    if (Date.now() - value.createdAt > this.lifetimeMs) { this.records.delete(token); return undefined; }
    return value.record;
  }

  get size() { return this.records.size; }
}
