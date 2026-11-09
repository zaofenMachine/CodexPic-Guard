import asyncio
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import aiohttp
from aiohttp import web
from codexpic_guard.config import Config,Policy,GuardError
from codexpic_guard.proxy import make_app,pack,unpack
from codexpic_guard.rewrite import refs_in
from tests.helpers import picture,request


async def listen(app):
    runner=web.AppRunner(app,access_log=None);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    return runner,site._server.sockets[0].getsockname()[1]


class CodecTest(unittest.TestCase):
    def test_encodings_roundtrip_and_expansion_limit(self):
        raw=b'hello'*5000
        for encoding in ('identity','gzip','deflate','zstd'):
            encoded=pack(raw,encoding);self.assertEqual(unpack(encoded,encoding,100000),raw)
            with self.assertRaises(GuardError):unpack(encoded,encoding,100)
        with self.assertRaises(GuardError):unpack(b'bad','zstd',1000)
        with self.assertRaises(GuardError):unpack(b'bad','br',1000)


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.hits=[];self.responses=[]
        async def backend(req):
            data=await req.read();decoded=unpack(data,req.headers.get('Content-Encoding'),20*1024*1024)
            self.hits.append({'path':req.path,'body':decoded,'headers':dict(req.headers)})
            if req.path.endswith('/redirect') or req.query.get('redirect'):
                return web.Response(status=307,headers={'Location':'http://127.0.0.1:1/should-not-follow'})
            if req.path.endswith('/models'):return web.json_response({'models':[]})
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(req)
            await response.write(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n')
            await asyncio.sleep(.02)
            await response.write(b'event: response.completed\ndata: {"type":"response.completed"}\n\n')
            return response
        back=web.Application(client_max_size=20*1024*1024,handler_args={'auto_decompress':False});back.router.add_route('*','/{path:.*}',backend)
        self.back_runner,self.back_port=await listen(back)
        policy=Policy(image_target_bytes=40000,media_high_bytes=250000,media_low_bytes=140000,max_images=6,keep_recent=2)
        self.config=Config(data_dir=str(Path(self.temp.name)/'cache'),upstream_base=f'http://127.0.0.1:{self.back_port}/v1',local_prefix='/api',allow_direct=False,policy=policy)
        self.app=make_app(self.config);self.runner,self.port=await listen(self.app)
        self.client=aiohttp.ClientSession(trust_env=False)
    async def asyncTearDown(self):
        await self.client.close();await self.runner.cleanup();await self.back_runner.cleanup();self.temp.cleanup()

    async def test_identity_gzip_zstd_requests_and_streamed_response(self):
        images=[picture(i,(300,200)) for i in range(10)]
        for encoding in ('identity','gzip','zstd'):
            raw=json.dumps(request(images)).encode();body=pack(raw,encoding)
            async with self.client.post(f'http://127.0.0.1:{self.port}/api/responses',data=body,
                headers={'Content-Encoding':encoding,'Content-Type':'application/json','session_id':encoding,'Authorization':'Bearer TEST_AUTH_NOT_LOGGED'}) as response:
                self.assertEqual(response.status,200);self.assertIn(b'response.completed',await response.read())
            got=json.loads(self.hits[-1]['body'])
            self.assertLessEqual(len(refs_in(got)),6)
            self.assertEqual(self.hits[-1]['headers']['Authorization'],'Bearer TEST_AUTH_NOT_LOGGED')
        self.assertEqual(self.app['store'].status()['requests'],3)
        for p in Path(self.temp.name).rglob('*'):
            if p.is_file():self.assertNotIn(b'TEST_AUTH_NOT_LOGGED',p.read_bytes())

    async def test_websocket_falls_back_without_forwarding(self):
        async with self.client.get(f'http://127.0.0.1:{self.port}/api/responses',headers={'Upgrade':'websocket','Connection':'Upgrade'}) as r:
            self.assertEqual(r.status,426)
        self.assertFalse(self.hits)

    async def test_plain_text_request_is_not_reserialized(self):
        body=b'{ "model":"test", "input":"text stays exactly the same", "stream":true }'
        async with self.client.post(f'http://127.0.0.1:{self.port}/api/responses',data=body,headers={'Content-Type':'application/json'}) as r:await r.read();self.assertEqual(r.status,200)
        self.assertEqual(self.hits[-1]['body'],body)

    async def test_redirect_not_followed(self):
        async with self.client.post(f'http://127.0.0.1:{self.port}/api/redirect',data=b'{}',allow_redirects=False) as r:self.assertEqual(r.status,307)
        self.assertEqual(len(self.hits),1)

    async def test_model_redirect_cannot_bypass_the_guard(self):
        async with self.client.post(f'http://127.0.0.1:{self.port}/api/responses?redirect=1',json={'input':'test'},allow_redirects=True) as r:
            self.assertEqual(r.status,502);self.assertEqual((await r.json())['error']['code'],'upstream_redirect_refused')

    async def test_cross_origin_and_host_rebinding_rejected(self):
        for headers in ({'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            async with self.client.get(f'http://127.0.0.1:{self.port}/_guard/health',headers=headers) as r:self.assertEqual(r.status,403)

    async def test_failure_does_not_fall_back_to_direct(self):
        config=Config(data_dir=str(Path(self.temp.name)/'closed'),upstream_base='https://no-service.invalid/v1',local_prefix='/api',upstream_proxy='http://127.0.0.1:1')
        runner,port=await listen(make_app(config))
        try:
            async with self.client.post(f'http://127.0.0.1:{port}/api/responses',json={'input':'test'}) as r:
                self.assertEqual(r.status,502);self.assertEqual((await r.json())['error']['code'],'upstream_unavailable')
        finally:await runner.cleanup()

    async def test_compact_request_is_budgeted_too(self):
        body=request([picture(i,(300,200)) for i in range(10)])
        async with self.client.post(f'http://127.0.0.1:{self.port}/api/responses/compact',json=body,headers={'session_id':'compact'}) as r:
            self.assertEqual(r.status,200);await r.read()
        self.assertLessEqual(len(refs_in(json.loads(self.hits[-1]['body']))),6)

    async def test_invalid_image_is_not_forwarded_unmodified(self):
        b={'input':[{'role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,aGVsbG8='}]}]}
        async with self.client.post(f'http://127.0.0.1:{self.port}/api/responses',json=b) as r:self.assertEqual(r.status,400)
        self.assertFalse(self.hits)
