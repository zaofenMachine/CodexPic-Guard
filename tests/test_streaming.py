import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web
from codexpic_guard.config import Config
from codexpic_guard.images import decode_url
from codexpic_guard.proxy import make_app
from codexpic_guard.store import scope_id
from codexpic_guard.streaming import ResponseCompletion
from tests.helpers import image_block, picture, request
from tests.test_proxy import listen


class CompletionTest(unittest.TestCase):
    def test_split_crlf_multiline_event(self):
        tracker = ResponseCompletion()
        stream = b'event: response.completed\r\ndata:{"type":\r\ndata: "response.completed"}\r\n\r\n'
        for start in range(0, len(stream), 3):
            tracker.feed(stream[start:start + 3])
        self.assertTrue(tracker.successful)

    def test_text_mention_and_incomplete_event_are_not_completion(self):
        for stream in (b'data:{"type":"response.output_text.delta","delta":"response.completed"}\n\n',
                       b'event: response.completed\ndata:{"type":"response.completed"}',
                       b'data:{"type":"response.failed"}\n\n'):
            tracker = ResponseCompletion(); tracker.feed(stream)
            self.assertFalse(tracker.successful)

    def test_oversized_event_does_not_grow_unbounded_or_acknowledge(self):
        tracker = ResponseCompletion(limit=100)
        tracker.feed(b'data:' + b'x' * 101)
        tracker.feed(b'\n\ndata:{"type":"response.completed"}\n\n')
        self.assertFalse(tracker.successful)
        self.assertFalse(tracker.buffer)


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_event_before_disconnect_acknowledges_only_success(self):
        for event_type in ('response.completed', 'response.failed', 'response.incomplete', None):
            with self.subTest(event=event_type), tempfile.TemporaryDirectory() as d:
                sent = asyncio.Event(); finish = asyncio.Event()
                async def backend(req):
                    await req.read()
                    # No Content-Type, matching the observed live endpoint.
                    response = web.StreamResponse(); await response.prepare(req)
                    event = {'type': event_type or 'response.output_text.delta', 'delta': 'test'}
                    await response.write(('data:' + json.dumps(event) + '\n\n').encode())
                    sent.set(); await finish.wait()
                    if req.transport:
                        req.transport.close()
                    return response
                backend_app = web.Application(); backend_app.router.add_post('/v1/responses', backend)
                back_runner, back_port = await listen(backend_app)
                cfg = Config(data_dir=str(Path(d) / 'cache'), upstream_base=f'http://127.0.0.1:{back_port}/v1', local_prefix='/v1')
                app = make_app(cfg); runner, port = await listen(app)
                store = app['store']; scope = scope_id('stream-probe')
                raw = picture(92, (100, 100))
                image_id, _ = store.register(decode_url(image_block(raw)['image_url'], cfg.policy), scope, mark_pending=True)
                store.advance(scope); store.grant_original(scope, image_id)
                try:
                    async with aiohttp.ClientSession(trust_env=False) as client:
                        response = await client.post(f'http://127.0.0.1:{port}/v1/responses',
                            json=request([raw]), headers={'thread-id': 'stream-probe'})
                        await sent.wait()
                        await response.content.readuntil(b'\n\n')
                        response.close()
                        # The acknowledgement must happen before upstream EOF.
                        for _ in range(50):
                            if event_type != 'response.completed' or not store.pending(scope):
                                break
                            await asyncio.sleep(.01)
                        expected = set() if event_type == 'response.completed' else {image_id}
                        self.assertEqual(store.pending(scope), expected)
                        self.assertEqual(store.original_grants(scope), expected)
                    finish.set()
                finally:
                    finish.set()
                    await runner.cleanup(); await back_runner.cleanup()
