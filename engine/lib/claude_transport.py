#!/usr/bin/env python3
"""Owned Claude CLI transport: every Messages request is reserved in the ledger before HTTPS; the listener is open only while the child CLI lives.
Credentials are never read or logged; unsupported transports, unknown prices and quota fail closed."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import http.server
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from butce_kapisi import BudgetDenied, BudgetLedger, amount, kosu_harcama_yaz, note_denial, observed_budget, rol_butcesi
from kosu import owner_fds


def micro(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Pricing:
    input: Decimal
    output: Decimal
    maximum: int
    # B73: receipt rates (real price); `input` stays 2x safe for the reservation
    settle_input: Decimal = Decimal(0)
    cache_write: Decimal = Decimal(0)
    cache_read: Decimal = Decimal(0)
    # measured: the real invoice of the plan call was $0.4738, our ledger recorded $0.3193 (32% short).
    # Cause: 41204 tokens were written to a ONE-HOUR cache; a one-hour write is TWICE the input price, while the catalogue's
    # `cache_write` is the FIVE-MINUTE rate (1.25x). The two must be priced separately.
    cache_write_1h: Decimal = Decimal(0)


def price(body: dict[str, Any], catalog: dict[str, Any]) -> Pricing:
    try:
        model = catalog['anthropic']['models'][body['model']]
        cap = body['max_tokens']
        context = model['limit']['context']
        if type(cap) is not int or type(context) is not int or not 0 < cap <= model['limit']['output'] or context <= 0:
            raise BudgetDenied('invalid token limit')
        if body.get('speed', 'standard') != 'standard' or body.get('service_tier', 'auto') not in ('auto', 'standard_only') or body.get('inference_geo', 'global') != 'global':
            raise BudgetDenied('unpriced service tier')
        if any(tool.get('type', 'custom') != 'custom' for tool in body.get('tools', [])):
            raise BudgetDenied('unpriced server-side tool')
        costs = [model['cost']]
        if model['cost'].get('context_over_200k'):
            costs.append(model['cost']['context_over_200k'])
        # A one-hour prompt-cache write can cost twice the normal input rate.
        in_rate = max(Decimal(str(amount(c['input']))) * 2 for c in costs)
        in_rate = max(in_rate, *(Decimal(str(amount(c.get(key, 0)))) for c in costs for key in ('cache_write','cache_read')))
        out_rate = max(Decimal(str(amount(c['output']))) for c in costs)
        if in_rate <= 0 or out_rate <= 0:
            raise BudgetDenied('unknown model price')
    # The upper bound is priced by the size of the REQUEST, not by the model's FULL context; otherwise a single call
    # reserved the whole cap and got the next one denied (B56).
        istek = min(context, len(json.dumps(body).encode('utf-8')) + 4096)
        # B73 (T2): the reservation uses the 2x safe in_rate; the RECEIPT closes with the real rates (otherwise spending in run.json
        # looked ~2x the real invoice and a $5 cap was effectively full at ~$2.5).
        gercek_in = max(Decimal(str(amount(c['input']))) for c in costs)
        cache_w = max((Decimal(str(amount(c['cache_write']))) for c in costs if c.get('cache_write')), default=gercek_in)
        cache_r = max((Decimal(str(amount(c['cache_read']))) for c in costs if c.get('cache_read')), default=gercek_in)
        return Pricing(in_rate, out_rate, micro(istek * in_rate + cap * out_rate), gercek_in, cache_w, cache_r,
                       gercek_in * 2)   # one-hour cache write: twice the input price
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise BudgetDenied('unknown model price or request shape') from exc


def kalibre_tavan(snapshot: Any, dilim: str | None, worst: int) -> int:
    """B253: if the bucket has a calibrated cap (USD -> micro), the smaller of it and the worst case; otherwise the worst case."""
    try:
        k = (snapshot or {}).get('kalibre', {}).get(dilim or 'seritler', {}).get('claude')
        return min(worst, int(Decimal(str(k)) * 1_000_000)) if k else worst
    except (TypeError, ValueError, InvalidOperation, AttributeError):
        return worst


class Relay:
    def __init__(self, home: Path, catalog: dict[str, Any], budget: Callable[[], Any], upstream: Callable[..., Any]):
        self.home = home
        self.ledger = BudgetLedger(home/'zincir/request-budget.db')
        self.catalog, self.budget, self.upstream = catalog, budget, upstream

    def open(self, body: dict[str, Any], headers: dict[str, str], query: str = '') -> tuple[str, Pricing, Any]:
        try:
            if query not in ('', 'beta=true'):
                raise BudgetDenied('unsupported API query')
            pricing = price(body, self.catalog)
            run_id = os.environ.get('CADRE_RUN_ID')
            # B79: this saw the WHOLE run via `run_cap_micro`; judge/helper calls coming through Claude
            # stayed outside the hard split and were charged to the lanes' pocket.
            tavan, dilim_adi = rol_butcesi(self.home, run_id,
                                           os.environ.get('CADRE_CLAUDE_ROL') or os.environ.get('CADRE_ROL'), 'claude')
            snapshot = self.budget()
            # B253 (OBSERVATION-6): if the observation has a calibrated cap for the bucket (real p95 x1.5), hold the smaller one instead of the worst case;
            # the receipt still closes with real usage, an overrun is limited to the difference of one call.
            maximum = kalibre_tavan(snapshot, dilim_adi, pricing.maximum)
            reservation = self.ledger.reserve('claude', maximum, snapshot,
                {'adapter':'claude-http', 'model':body['model'], 'run_id':run_id,
                 'task':os.environ.get('CADRE_GOREV_ID') or 'main',   # B72: the breakdown reads 'task'/'role' (same names as the plugin)
                 'role':os.environ.get('CADRE_CLAUDE_ROL') or 'ana',
                 'task_id':os.environ.get('CADRE_GOREV_ID') or 'main',
                 'attempt_id':os.environ.get('CADRE_BUDGET_ATTEMPT')}, run_cap=tavan, dilim=dilim_adi)
            kosu_harcama_yaz(self.home, self.ledger, run_id)
        except BudgetDenied as exc:
            note_denial(self.home, 'claude', reason=str(exc))
            raise
        body = {**body, 'service_tier':'standard_only'}
        return reservation, pricing, self.upstream(body, headers, query)


class Receipt:
    def __init__(self, ledger: BudgetLedger, ident: str, pricing: Pricing, sse: bool):
        self.ledger, self.ident, self.pricing, self.sse = ledger, ident, pricing, sse
        self.buffer = b''
        self.input: int | None = None
        self.taze = self.cache_w = self.cache_r = self.cache_w1h = 0
        self.output: int | None = None
        self.receipt = ''
        self.complete = False
        self.invalid = False

    def item(self, raw: bytes) -> None:
        try:
            event = json.loads(raw)
            kind = event.get('type')
            message = event['message'] if kind == 'message_start' else event
            if kind in ('message_start', 'message'):
                usage = message['usage']
                counts = [usage['input_tokens'], usage.get('cache_creation_input_tokens', 0), usage.get('cache_read_input_tokens', 0)]
                if any(type(n) is not int or n < 0 for n in counts):
                    raise ValueError('invalid input usage')
                self.input = sum(counts)
                self.taze, self.cache_w, self.cache_r = counts  # B73: the receipt closes with the real rates
                # One-hour and five-minute cache writes are priced SEPARATELY; the API gives the breakdown under
                # `cache_creation`. Without a breakdown all count as five-minute (old behaviour) — but if 1h is present the expensive rate applies.
                kirilim = usage.get('cache_creation') or {}
                bir_saat = kirilim.get('ephemeral_1h_input_tokens', 0)
                self.cache_w1h = bir_saat if type(bir_saat) is int and 0 <= bir_saat <= self.cache_w else 0
                self.cache_w = self.cache_w - self.cache_w1h
                self.receipt = message['id']
            usage = message.get('usage', {})
            if 'output_tokens' in usage:
                count = usage['output_tokens']
                if type(count) is not int or count < 0:
                    raise ValueError('invalid output usage')
                self.output = count
            if kind in ('message_stop', 'message'):
                self.complete = True
            if kind == 'error':
                self.invalid = True
        except (ValueError, TypeError, KeyError):
            self.invalid = True

    def feed(self, data: bytes) -> None:
        if self.invalid:
            return
        self.buffer += data
        if len(self.buffer) > 4 * 1024 * 1024:
            self.invalid = True
            self.buffer = b''
        elif self.sse:
            lines = self.buffer.split(b'\n')
            self.buffer = lines.pop()
            for line in lines:
                if line.startswith(b'data:'):
                    self.item(line[5:].strip())

    def finish(self) -> None:
        if not self.sse:
            self.item(self.buffer)
        elif self.buffer.startswith(b'data:'):
            self.item(self.buffer[5:].strip())
        if self.invalid or not self.complete or self.input is None or self.output is None or not self.receipt:
            return
        p = self.pricing
        if p.settle_input:  # B73: real price (fresh input + cache write + cache read at separate rates)
            actual = micro(self.taze * p.settle_input + self.cache_w * p.cache_write
                           + self.cache_w1h * (p.cache_write_1h or p.cache_write) + self.cache_r * p.cache_read
                           + self.output * p.output)
        else:
            actual = micro(self.input * p.input + self.output * p.output)
        self.ledger.settle(self.ident, actual, self.ident + ':' + self.receipt)
        kosu_harcama_yaz(self.ledger.path.parents[1], self.ledger, os.environ.get('CADRE_RUN_ID'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def upstream(body: dict[str, Any], headers: dict[str, str], query: str = '') -> Any:
    request = urllib.request.Request('https://api.anthropic.com/v1/messages' + ('?' + query if query else ''),
                                     data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=180)
    except urllib.error.HTTPError as response:
        return response  # Forward the failure; its charge remains unknown.


def handler_for(relay: Relay, token: str) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass  # Never log URLs containing the loopback capability.

        def do_POST(self) -> None:
            if self.path.split('?', 1)[0] != f'/{token}/v1/messages':
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 64 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                    raise BudgetDenied('unsupported request framing')
                self.connection.settimeout(30)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise BudgetDenied('incomplete request')
                body = json.loads(raw)
                headers = {k:v for k,v in self.headers.items() if k.lower() not in
                           ('host','connection','content-length','transfer-encoding','accept-encoding')}
                headers['Accept-Encoding'] = 'identity'
                ident, pricing, response = relay.open(body, headers, self.path.partition('?')[2])
            except BudgetDenied as exc:
                # B244: the reason is only known here; the engine log (stderr) gets one line so the RCA can tell pocket/share/stale apart.
                print(f"CADRE_BUDGET_DENIED: {exc}", file=sys.stderr, flush=True)
                payload = b'{"type":"error","error":{"type":"permission_error","message":"CADRE_BUDGET_DENIED"}}'
                self.send_response(403)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except Exception:
                self.send_error(502, 'upstream unavailable; reservation retained')
                return
            try:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in ('connection','content-length','transfer-encoding'):
                        self.send_header(key, value)
                self.send_header('Connection','close')
                self.end_headers()
                if 400 <= response.status < 600:
                    # No tokens are billed on 4xx and 5xx: the reservation CLOSES at zero. If NO response came at all
                    # the outcome is unknown and the reservation is kept (B68/B110).
                    try:
                        relay.ledger.settle(ident, 0, f'{ident}:http-{response.status}')
                        kosu_harcama_yaz(relay.home, relay.ledger, os.environ.get('CADRE_RUN_ID'))
                    except Exception:
                        pass
                parser = Receipt(relay.ledger, ident, pricing, 'text/event-stream' in response.headers.get('Content-Type',''))
                while data := response.read1(65536):
                    self.wfile.write(data)
                    self.wfile.flush()
                    parser.feed(data)
                if response.status == 200:
                    parser.finish()
            except Exception:
                pass  # Partial delivery/receipt failure retains the reservation.
            finally:
                response.close()
                self.close_connection = True

    return Handler


def server(relay: Relay, token: str) -> http.server.ThreadingHTTPServer:
    listener = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler_for(relay, token))
    listener.daemon_threads = True
    return listener


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    home = Path(os.environ['CADRE_HOME'])
    code = Path(os.environ['CADRE_ENGINE_ROOT'])
    # These alternate transports bypass ANTHROPIC_BASE_URL; do not admit them.
    if any(os.environ.get(key) not in (None, '', '0', 'false') for key in
           ('CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY')):
        note_denial(home, 'claude')
        return 75
    if os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com').rstrip('/') != 'https://api.anthropic.com':
        note_denial(home, 'claude')
        return 75
    catalog_path = Path(os.environ.get('CADRE_MODEL_KATALOG', str(Path.home()/'.cache/opencode/models.json')))
    try:
        if not 0 <= time.time() - catalog_path.stat().st_mtime < 7 * 86400:
            raise BudgetDenied(f'model catalogue stale: {catalog_path} is {int((time.time() - catalog_path.stat().st_mtime) // 86400)} days old')
        catalog = json.loads(catalog_path.read_text())
        token = secrets.token_urlsafe(32)
        listener = server(Relay(home, catalog, lambda:observed_budget(home, code), upstream), token)
    except (OSError, ValueError, BudgetDenied) as exc:
        # P5: the reason is structured (run.json denials[]) — "why did Claude never run" is read from the record in the morning, not from the log.
        note_denial(home, 'claude', reason=str(exc) if isinstance(exc, BudgetDenied) else f'model catalogue unreadable: {type(exc).__name__}')
        return 75
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, 'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{listener.server_port}/{token}'}
    child: subprocess.Popen[bytes] | None = None
    cancelled = False
    def stop(_signum: int, _frame: Any) -> None:
        nonlocal cancelled
        cancelled = True
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        if cancelled:
            return 130
        child = subprocess.Popen(sys.argv[1:], env=env, pass_fds=owner_fds())
        if cancelled:
            stop(signal.SIGTERM, None)
        return child.wait()
    finally:
        stop(signal.SIGTERM, None)
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
