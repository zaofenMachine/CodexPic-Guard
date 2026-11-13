"""Opt-in synthetic visual transport pilot using Codex's existing ChatGPT login.

Each arm is capped at eight generation requests and 180 seconds. No credentials
are copied, no live settings are edited, and only generated fixtures are sent.
Reports measure application payloads and reported token usage, not VPN billing.
"""
import argparse
import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from aiohttp import web, WSMsgType
from PIL import Image, ImageDraw, ImageFont
from codexpic_guard.config import Config, Policy, loopback
from codexpic_guard.images import decode_url, normalize
from codexpic_guard.rewrite import refs_in, rewrite
from codexpic_guard.install import codex_home, default_data
from codexpic_guard.proxy import filtered_headers, make_app, pack, unpack
from experiments.transport_probe import describe
from tests.native_codex_smoke import free_port

CODES = ['Q7M2', 'K4P8', 'R9T3']


def fixtures(root):
    font = ImageFont.truetype('arial.ttf' if os.name == 'nt' else 'DejaVuSans.ttf', 100)
    for step, code in enumerate(CODES):
        rng = random.Random(7100 + step)
        data = bytes(195 + rng.randrange(50) for _ in range(1200 * 800 * 3))
        image = Image.frombytes('RGB', (1200, 800), data)
        draw = ImageDraw.Draw(image)
        draw.rectangle((220, 200, 980, 600), fill='white', outline='black', width=8)
        draw.text((430, 330), code, fill='black', font=font)
        image.save(root / f'frame-{step}.png')


def fixture_server(root):
    step = 0
    tool = {'name': 'probe_next_frame',
            'description': 'Read the large four-character code in the current image, then call this tool with that code. Returns the next image. Stop when it says complete. Inspect each image before advancing; do not inspect local files.',
            'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}},
                            'required': ['code'], 'additionalProperties': False},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}
    for line in sys.stdin.buffer:
        req = json.loads(line)
        if 'id' not in req:
            continue
        method = req.get('method'); params = req.get('params') or {}
        if method == 'initialize':
            result = {'protocolVersion': params.get('protocolVersion', '2025-06-18'),
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'visual-probe', 'version': '1.0'}}
        elif method == 'tools/list':
            result = {'tools': [tool]}
        elif method == 'tools/call':
            code = params.get('arguments', {}).get('code', '')
            correct = step < len(CODES) and params.get('name') == tool['name'] and code == CODES[step]
            with (root / 'fixture-calls.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps({'step': step, 'correct': correct}) + '\n')
            if not correct:
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'Incorrect code. Inspect the current image again.'}]}
            else:
                step += 1
                if step == len(CODES):
                    result = {'content': [{'type': 'text', 'text': 'VISUAL_PROBE_COMPLETE: all three image codes correct. Reply VISUAL_PROBE_OK.'}]}
                else:
                    raw = (root / f'frame-{step}.png').read_bytes()
                    result = {'content': [{'type': 'text', 'text': 'Inspect this next frame, then advance with its visible code.'},
                                          {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(raw).decode()}]}
        else:
            result = {}
        print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)


class Observation:
    def __init__(self):
        self.requests = []; self.responses = []; self.handshakes = []
        self.http_responses = []
        self.sse_buffer = b''; self.generation_requests = 0

    def request(self, body, count, transport, encoding):
        if body.get('generate') is not False:
            self.generation_requests += 1
        if self.generation_requests > 8:
            raise web.HTTPTooManyRequests(text='Synthetic pilot request limit reached')
        self.requests.append(describe(body, count, transport, encoding))

    def event(self, event):
        if event.get('type') in ('response.completed', 'response.failed', 'response.incomplete', 'error'):
            response = event.get('response') or {}
            error = event.get('error') or response.get('error') or {}
            usage = response.get('usage')
            if usage:
                usage = {k: v for k, v in usage.items() if k in (
                    'input_tokens', 'output_tokens', 'total_tokens',
                    'input_tokens_details', 'output_tokens_details')}
            self.responses.append({'type': event['type'], 'status': response.get('status'),
                                   'usage': usage, 'error_code': error.get('code')})

    def sse(self, chunk):
        self.sse_buffer += chunk
        if len(self.sse_buffer) > 4 * 1024 * 1024:
            raise RuntimeError('Unexpectedly large SSE event')
        while b'\n' in self.sse_buffer:
            line, self.sse_buffer = self.sse_buffer.split(b'\n', 1)
            if line.startswith(b'data:'):
                try:
                    self.event(json.loads(line[5:].strip()))
                except ValueError:
                    pass


def preview_request(body, cache, policy):
    """Preview only: no historical eviction or change to response lineage."""
    changed = 0
    for ref in refs_in(body):
        source = decode_url(ref.block[ref.field], policy)
        if source.sha not in cache:
            cache[source.sha] = normalize(source, policy).url
        preview = cache[source.sha]
        if preview != ref.block[ref.field]:
            ref.block[ref.field] = preview
            changed += 1
    return changed


def cli_usage(stdout):
    rows = [json.loads(line) for line in stdout.decode('utf-8').splitlines() if line.strip()]
    turns = [r['usage'] for r in rows if r.get('type') == 'turn.completed' and r.get('usage')]
    if not turns:
        return None
    totals = {k: sum(t.get(k, 0) for t in turns) for k in (
        'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
        'output_tokens', 'reasoning_output_tokens')}
    totals['uncached_input_tokens'] = totals['input_tokens'] - totals['cached_input_tokens']
    return totals


def stable_note_rewrite(body, headers, store, policy):
    """Ablation: move only the generated scope note to the first media group."""
    refs = refs_in(body)
    anchor = min((r.group for r in refs), default=0)
    items = body.get('input')
    if refs and items[anchor].get('type') in ('function_call_output', 'custom_tool_call_output', 'computer_call_output'):
        call_id = items[anchor].get('call_id')
        anchor = next((i for i, item in enumerate(items[:anchor]) if item.get('call_id') == call_id), anchor)
    out, scope, stats = rewrite(body, headers, store, policy)
    if isinstance(out.get('input'), list) and out['input'] and store.has_images(scope):
        note = out['input'].pop()
        out['input'].insert(anchor, note)
    return out, scope, stats


async def relay_app(config, observation, allow_ws, preview=False):
    app = web.Application(client_max_size=32 * 1024 * 1024, handler_args={'auto_decompress': False})
    client = aiohttp.ClientSession(auto_decompress=False, trust_env=False,
                                  timeout=aiohttp.ClientTimeout(total=None, connect=25, sock_read=90))
    preview_cache = {}
    async def cleanup(app):
        await client.close()
    app.on_cleanup.append(cleanup)

    async def handle(req):
        if not loopback(urlsplit('//' + req.host).hostname or '') or req.headers.get('Origin'):
            raise web.HTTPForbidden()
        prefix = '/backend-api/codex'
        if not req.path.startswith(prefix + '/'):
            raise web.HTTPNotFound()
        target = config.upstream_base.rstrip('/') + req.path[len(prefix):]
        if req.query_string:
            target += '?' + req.query_string
        headers = filtered_headers(req.headers)
        if req.headers.get('Upgrade', '').lower() == 'websocket':
            if not allow_ws:
                observation.handshakes.append(426)
                return web.json_response({'error': {'message': 'HTTP control arm'}}, status=426)
            for key in list(headers):
                if key.lower().startswith('sec-websocket-'):
                    del headers[key]
            try:
                upstream = await client.ws_connect(target.replace('https:', 'wss:', 1), headers=headers,
                                                   proxy=config.upstream_proxy, compress=0,
                                                   max_msg_size=32 * 1024 * 1024)
            except aiohttp.WSServerHandshakeError as error:
                observation.handshakes.append(error.status)
                return web.json_response({'error': {'message': 'Upstream WebSocket handshake rejected'}}, status=error.status)
            observation.handshakes.append(101)
            downstream = web.WebSocketResponse(compress=False, max_msg_size=32 * 1024 * 1024)
            await downstream.prepare(req)
            async def send():
                async for msg in downstream:
                    if msg.type == WSMsgType.TEXT:
                        body = json.loads(msg.data)
                        original_bytes = len(msg.data.encode())
                        changed = preview_request(body, preview_cache, config.policy) if preview else 0
                        outgoing = json.dumps(body, ensure_ascii=False, separators=(',', ':')) if changed else msg.data
                        observation.request(body, len(outgoing.encode()), 'websocket', 'identity')
                        observation.requests[-1].update(before_payload_bytes=original_bytes,
                                                        images_previewed=changed)
                        await upstream.send_str(outgoing)
                    elif msg.type == WSMsgType.BINARY:
                        raise RuntimeError('Unexpected binary client frame')
            async def receive():
                async for msg in upstream:
                    if msg.type == WSMsgType.TEXT:
                        try:
                            observation.event(json.loads(msg.data))
                        except ValueError:
                            pass
                        await downstream.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await downstream.send_bytes(msg.data)
            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await upstream.close(); await downstream.close()
            return downstream
        data = await req.read()
        if req.method == 'POST' and req.path.endswith('/responses'):
            encoding = req.headers.get('Content-Encoding', 'identity')
            body = json.loads(unpack(data, encoding, 32 * 1024 * 1024))
            original_bytes = len(data)
            changed = preview_request(body, preview_cache, config.policy) if preview else 0
            if changed:
                data = pack(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode(), encoding)
                for key in list(headers):
                    if key.lower() in ('content-md5', 'digest', 'content-digest'):
                        del headers[key]
            observation.request(body, len(data), 'http', encoding)
            observation.requests[-1].update(before_payload_bytes=original_bytes, images_previewed=changed)
            observation.sse_buffer = b''
        headers['Accept-Encoding'] = 'identity'
        async with client.request(req.method, target, headers=headers, data=data or None,
                                  proxy=config.upstream_proxy, allow_redirects=False) as upstream:
            observation.http_responses.append({
                'status': upstream.status, 'content_type': upstream.headers.get('Content-Type'),
                'content_encoding': upstream.headers.get('Content-Encoding')})
            if 300 <= upstream.status < 400:
                raise web.HTTPBadGateway(text='Redirect refused')
            response = web.StreamResponse(status=upstream.status, headers=filtered_headers(upstream.headers))
            await response.prepare(req)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                if req.method == 'POST' and req.path.endswith('/responses') and not upstream.headers.get('Content-Encoding'):
                    observation.sse(chunk)
                await response.write(chunk)
            try:
                await response.write_eof()
            except aiohttp.ClientConnectionError:
                # Codex can close as soon as response.completed is received.
                pass
            return response
    app.router.add_route('*', '/{path:.*}', handle)
    return app


def file_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
            for p in (codex_home() / 'config.toml', codex_home() / '.env')}


async def run_arm(binary, root, arm, config, model):
    root.mkdir(parents=True, exist_ok=False); work = root / 'work'; work.mkdir()
    fixtures(work)
    obs = Observation(); relay = await relay_app(config, obs, arm in ('websocket', 'ws-preview'), arm in ('ws-preview', 'http-preview'))
    runner = web.AppRunner(relay, access_log=None); await runner.setup()
    relay_port = free_port(); await web.TCPSite(runner, '127.0.0.1', relay_port).start()
    base = f'http://127.0.0.1:{relay_port}/backend-api/codex'
    guard_runner = None; guard = None
    if arm in ('guard-http', 'guard-direct', 'guard-stable-note'):
        cfg = replace(config, data_dir=str(root / 'cache'), upstream_base=config.upstream_base if arm == 'guard-direct' else base,
                      local_prefix='/backend-api/codex', port=free_port())
        guard = make_app(cfg, rewriter=stable_note_rewrite if arm == 'guard-stable-note' else None)
        @web.middleware
        async def direct_limit(req, handler):
            if arm == 'guard-direct' and req.method == 'POST' and req.path.endswith('/responses'):
                obs.generation_requests += 1
                if obs.generation_requests > 8:
                    raise web.HTTPTooManyRequests(text='Synthetic pilot request limit reached')
            return await handler(req)
        guard.middlewares.append(direct_limit)
        guard_runner = web.AppRunner(guard, access_log=None)
        await guard_runner.setup(); await web.TCPSite(guard_runner, '127.0.0.1', cfg.port).start()
        base = cfg.local_base
    env = dict(os.environ, NO_PROXY='127.0.0.1,localhost,::1')
    for key in ('OPENAI_BASE_URL', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'no_proxy'):
        env.pop(key, None)
    before = file_hashes()
    server_args = [str(Path(__file__).resolve()), 'mcp', str(work)]
    args = [str(binary), 'exec', '--ignore-user-config', '--skip-git-repo-check', '--ephemeral', '--json',
            '-C', str(work), '-s', 'read-only', '-m', model,
            '-c', 'approval_policy="never"', '-c', 'model_reasoning_effort="low"',
            '-c', 'analytics.enabled=false', '-c', 'features.respect_system_proxy=true',
            '-c', 'openai_base_url=' + json.dumps(base),
            '-c', 'mcp_servers.visual_probe.command=' + json.dumps(sys.executable),
            '-c', 'mcp_servers.visual_probe.args=' + json.dumps(server_args),
            '-c', 'mcp_servers.visual_probe.required=true',
            '-i', str(work / 'frame-0.png'), '--',
            'Perform this synthetic visual probe. Read the large four-character code in the attached image. '
            'Call probe_next_frame with that code, inspect the returned image, and repeat until the tool says '
            'VISUAL_PROBE_COMPLETE. Then reply VISUAL_PROBE_OK only. Do not read files, execute shell commands, '
            'browse, or use any other tool. Each next frame must be visually inspected before its next call.']
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 180)
        except asyncio.TimeoutError:
            timed_out = True; proc.kill(); stdout, stderr = await proc.communicate()
        (root / 'stdout.jsonl').write_bytes(stdout); (root / 'stderr.txt').write_bytes(stderr)
        calls_path = work / 'fixture-calls.jsonl'
        calls = [json.loads(x) for x in calls_path.read_text().splitlines()] if calls_path.exists() else []
        usages = [r['usage'] for r in obs.responses if r['usage']]
        total_input = sum(u.get('input_tokens', 0) for u in usages)
        cached = sum(u.get('input_tokens_details', {}).get('cached_tokens', 0) for u in usages)
        client_usage = cli_usage(stdout)
        result = {'arm': arm, 'model': model, 'exit_code': proc.returncode, 'timed_out': timed_out,
                  'seconds': round(time.perf_counter() - started, 3),
                  'global_configuration_unchanged': before == file_hashes(),
                  'passed': proc.returncode == 0 and len(calls) == 3 and all(c['correct'] for c in calls)
                            and b'VISUAL_PROBE_OK' in stdout and before == file_hashes(),
                  'visual_calls': calls, 'ws_handshakes': obs.handshakes,
                  'http_responses': obs.http_responses,
                  'generation_requests': obs.generation_requests,
                  'request_payload_bytes': sum(r['encoded_payload_bytes'] for r in obs.requests),
                  'image_base64_bytes': sum(r['image_base64_bytes'] for r in obs.requests),
                  'usage': client_usage, 'usage_source': 'Codex turn.completed; excludes WS prewarm',
                  'observed_response_usage': {'input_tokens': total_input, 'cached_input_tokens': cached,
                            'uncached_input_tokens': total_input - cached,
                            'output_tokens': sum(u.get('output_tokens', 0) for u in usages)},
                  'requests': obs.requests, 'responses': obs.responses}
        if guard is not None:
            # Allow handlers to observe the client's terminal-event disconnect.
            await asyncio.sleep(.2)
            result['guard_metrics'] = guard['store'].status()
            with guard['store'].connect() as db:
                result['guard_recorded_statuses'] = [r[0] for r in db.execute('SELECT status FROM metrics ORDER BY id')]
                result['guard_pending_images'] = db.execute('SELECT count(*) FROM scoped_images WHERE pending=1').fetchone()[0]
            if arm == 'guard-direct':
                result['request_payload_bytes'] = result['guard_metrics']['output_body_bytes']
                result['image_base64_bytes'] = None
        (root / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items() if k not in ('requests', 'responses')}), flush=True)
        return result
    finally:
        if guard_runner:
            await guard_runner.cleanup()
        await runner.cleanup()


async def main(args):
    binary = Path(args.codex).resolve()
    login = subprocess.run([str(binary), 'login', 'status'], capture_output=True)
    if login.returncode or b'ChatGPT' not in login.stdout + login.stderr:
        raise SystemExit('An existing Codex ChatGPT login is required; credentials are not copied.')
    cfg = Config.load(default_data() / 'guard.json')
    url = urlsplit(cfg.upstream_base)
    if url.hostname != 'chatgpt.com' or url.path.rstrip('/') != '/backend-api/codex' or not cfg.upstream_proxy:
        raise SystemExit('Only the official ChatGPT Codex endpoint through an explicit proxy is allowed.')
    root = Path(args.out).resolve(); root.mkdir(parents=True, exist_ok=False)
    results = []
    for arm in args.arms:
        results.append(await run_arm(binary, root / arm, arm, cfg, args.model))
    summary = {'kind': 'single-run synthetic live pilot; not a cost benchmark',
               'wire_or_vpn_billing_measured': False,
               'codex_version': subprocess.check_output([str(binary), '--version'], text=True).strip(),
               'arms': results}
    (root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if not all(x['passed'] for x in results):
        raise SystemExit(1)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'mcp':
        fixture_server(Path(sys.argv[2]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--run', required=True, action='store_true')
        parser.add_argument('--codex', required=True)
        parser.add_argument('--out', required=True)
        parser.add_argument('--model', default='gpt-6-astra')
        parser.add_argument('--arms', nargs='+', choices=['websocket', 'http', 'guard-http', 'ws-preview', 'guard-direct', 'http-preview', 'guard-stable-note'],
                            default=['websocket', 'http', 'guard-http'])
        asyncio.run(main(parser.parse_args()))
