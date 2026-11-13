"""Measure native Codex upload payloads against local HTTP/WS mock services.

No real model, credentials, or live configuration changes. Token usage emitted
by the mock is deliberately synthetic and is not reported as a measurement.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time

from aiohttp import web, WSMsgType
from codexpic_guard.config import Config
from codexpic_guard.proxy import make_app, unpack
from codexpic_guard.rewrite import refs_in
from tests.helpers import picture
from tests.native_codex_smoke import free_port


def describe(body, encoded_bytes, transport, encoding):
    refs = refs_in(body)
    return {
        'transport': transport, 'encoded_payload_bytes': encoded_bytes,
        'encoding': encoding, 'input_items': len(body.get('input', [])),
        'image_blocks': len(refs),
        'image_base64_bytes': sum(len(r.block[r.field].split(',', 1)[1]) for r in refs),
        'previous_response_id_present': bool(body.get('previous_response_id')),
        'generate_false': body.get('generate') is False,
    }


def events(body, step, steps):
    if step <= steps:
        item = {'type': 'custom_tool_call', 'id': f'ctc_probe_{step}',
                'call_id': f'call_probe_{step}', 'name': 'exec', 'namespace': 'functions',
                'input': f'text("transport-step-{step}");'}
    else:
        item = {'type': 'message', 'id': 'msg_probe_done', 'role': 'assistant',
                'status': 'completed', 'phase': 'final_answer',
                'content': [{'type': 'output_text', 'text': 'TRANSPORT_PROBE_OK', 'annotations': []}]}
    response = {'id': f'resp_probe_{step}', 'object': 'response', 'created_at': int(time.time()),
                'model': body.get('model'), 'status': 'in_progress', 'output': []}
    return [
        {'type': 'response.created', 'response': response},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'response': {
            **response, 'status': 'completed', 'output': [item],
            'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                      'input_tokens_details': {'cached_tokens': 0}}}},
    ]


async def run_arm(binary, root, arm, steps):
    root.mkdir(parents=True, exist_ok=False)
    home = root / 'home'; home.mkdir()
    work = root / 'work'; work.mkdir()
    fixture = work / 'synthetic-noise.png'
    fixture.write_bytes(picture(709, (1000, 700)))
    rows = []; generated = 0; connections = 0; injected = False

    def reply(body, encoded, transport, encoding):
        nonlocal generated, injected
        row = describe(body, encoded, transport, encoding)
        rows.append(row)
        if len(rows) > steps + 6:
            raise RuntimeError('Mock request limit exceeded')
        if body.get('generate') is False:
            return [{'type': 'response.completed', 'response': {
                'id': 'resp_probe_warmup', 'status': 'completed', 'output': []}}]
        if arm == 'ws-cache-miss' and generated == 3 and not injected and body.get('previous_response_id'):
            injected = True; row['injected_cache_miss'] = True
            return [{'type': 'error', 'status': 400, 'error': {
                'type': 'invalid_request_error', 'code': 'previous_response_not_found',
                'message': 'Injected local mock cache miss.', 'param': 'previous_response_id'}}]
        generated += 1
        return events(body, generated, steps)

    async def backend(req):
        nonlocal connections
        if req.headers.get('Upgrade', '').lower() == 'websocket':
            connections += 1
            ws = web.WebSocketResponse(compress=False, max_msg_size=32 * 1024 * 1024)
            await ws.prepare(req)
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                body = json.loads(msg.data)
                for event in reply(body, len(msg.data.encode('utf-8')), 'websocket', 'identity'):
                    await ws.send_json(event)
            return ws
        if req.method == 'GET':
            return web.json_response({'models': []})
        wire = await req.read()
        encoding = req.headers.get('Content-Encoding', 'identity')
        body = json.loads(unpack(wire, encoding, 32 * 1024 * 1024))
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(req)
        for event in reply(body, len(wire), 'http', encoding):
            await response.write(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'.encode())
        await response.write_eof()
        return response

    app = web.Application(client_max_size=32 * 1024 * 1024, handler_args={'auto_decompress': False})
    app.router.add_route('*', '/{path:.*}', backend)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup(); upstream_port = free_port()
    await web.TCPSite(runner, '127.0.0.1', upstream_port).start()
    base = f'http://127.0.0.1:{upstream_port}/v1'
    guard_runner = None; guard = None
    if arm == 'guard-http':
        config = Config(data_dir=str(root / 'cache'), upstream_base=base,
                        local_prefix='/v1', port=free_port())
        guard = make_app(config); guard_runner = web.AppRunner(guard, access_log=None)
        await guard_runner.setup()
        await web.TCPSite(guard_runner, '127.0.0.1', config.port).start()
        base = config.local_base
    (home / 'config.toml').write_text(
        'model="gpt-6-astra"\nmodel_provider="probe"\napproval_policy="never"\n'
        '[analytics]\nenabled=false\n[features]\nrespect_system_proxy=true\n'
        '[model_providers.probe]\nname="Local transport probe"\n'
        f'base_url={json.dumps(base)}\nwire_api="responses"\nrequires_openai_auth=false\n'
        f'supports_websockets={"false" if arm == "http" else "true"}\n', encoding='utf-8')
    env = dict(os.environ, CODEX_HOME=str(home), NO_PROXY='127.0.0.1,localhost,::1',
               OPENAI_API_KEY='mock-no-real-key')
    for key in ('OPENAI_BASE_URL', 'CODEX_API_KEY', 'no_proxy'):
        env.pop(key, None)
    args = [str(binary), 'exec', '--strict-config', '--skip-git-repo-check', '--ephemeral',
            '--json', '-C', str(work), '-s', 'read-only', '-i', str(fixture), '--',
            'This is a local mock transport experiment. Complete the provided tool sequence.']
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 90)
        except asyncio.TimeoutError:
            proc.kill(); stdout, stderr = await proc.communicate()
        (root / 'stdout.jsonl').write_bytes(stdout)
        (root / 'stderr.txt').write_bytes(stderr)
        result = {'arm': arm, 'exit_code': proc.returncode,
                  'passed': proc.returncode == 0 and b'TRANSPORT_PROBE_OK' in stdout and generated == steps + 1,
                  'seconds': round(time.perf_counter() - started, 3),
                  'websocket_connections': connections, 'completed_model_steps': generated,
                  'request_payload_bytes': sum(r['encoded_payload_bytes'] for r in rows),
                  'image_base64_bytes': sum(r['image_base64_bytes'] for r in rows), 'requests': rows}
        if guard is not None:
            result['guard_metrics'] = guard['store'].status()
        (root / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items() if k != 'requests'}), flush=True)
        return result
    finally:
        if guard_runner:
            await guard_runner.cleanup()
        await runner.cleanup()


async def main(args):
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for arm in args.arms:
        results.append(await run_arm(Path(args.codex), root / arm, arm, args.steps))
    report = {'kind': 'native CLI against local synthetic mock only',
              'token_usage_measured': False, 'wire_or_vpn_billing_measured': False,
              'codex_version': subprocess.check_output([args.codex, '--version'], text=True).strip(),
              'steps': args.steps, 'arms': results}
    (root / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if not all(r['passed'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--steps', type=int, choices=range(1, 21), default=6)
    parser.add_argument('--arms', nargs='+', choices=['http', 'websocket', 'ws-cache-miss', 'guard-http'],
                        default=['http', 'websocket', 'ws-cache-miss', 'guard-http'])
    asyncio.run(main(parser.parse_args()))
