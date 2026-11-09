import asyncio
import gzip
import hmac
import io
import json
import logging
import ssl
import time
import zlib
from urllib.parse import urlsplit
import aiohttp
from aiohttp import web
import zstandard
from . import __version__
from .config import Config, GuardError, loopback
from .rewrite import rewrite
from .store import Store

HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','host','content-length'}


def unpack(data,encoding,limit):
    if len(data)>limit:raise GuardError('request_too_large','Encoded request exceeds the local limit.',413)
    encoding=(encoding or 'identity').lower().strip()
    try:
        if encoding in ('','identity'):raw=data
        elif encoding=='gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:raw=f.read(limit+1)
        elif encoding=='zstd':
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as f:raw=f.read(limit+1)
        elif encoding=='deflate':
            d=zlib.decompressobj();raw=d.decompress(data,limit+1)
            if d.unconsumed_tail:raise GuardError('request_too_large','Decoded request exceeds the local limit.',413)
        else:raise GuardError('unsupported_encoding','Supported request encodings are identity, gzip, deflate and zstd.',415)
    except GuardError:raise
    except Exception:raise GuardError('invalid_encoding','Request body decompression failed.',400) from None
    if len(raw)>limit:raise GuardError('request_too_large','Decoded request exceeds the local limit.',413)
    return raw


def pack(raw,encoding):
    encoding=(encoding or 'identity').lower().strip()
    if encoding=='gzip':return gzip.compress(raw,compresslevel=5,mtime=0)
    if encoding=='zstd':return zstandard.ZstdCompressor(level=3).compress(raw)
    if encoding=='deflate':return zlib.compress(raw,5)
    return raw


def filtered_headers(headers):
    hop=HOP|{x.strip().lower() for x in headers.get('Connection','').split(',')}
    return {k:v for k,v in headers.items() if k.lower() not in hop}


def make_app(config:Config,store=None):
    config.validate();store=store or Store(config.data_dir,config.policy)
    app=web.Application(client_max_size=config.max_request_bytes,handler_args={'auto_decompress':False})
    app['config']=config;app['store']=store;app['slots']=asyncio.Semaphore(config.concurrency)
    app['shutdown_event']=asyncio.Event()
    async def start(app):
        # Do not fall back from the configured proxy to DIRECT. aiohttp forwards via CONNECT for HTTPS.
        app['client']=aiohttp.ClientSession(auto_decompress=False,trust_env=False,
            timeout=aiohttp.ClientTimeout(total=None,connect=30,sock_read=config.upstream_timeout_seconds))
    async def stop(app):await app['client'].close()
    app.on_startup.append(start);app.on_cleanup.append(stop)

    async def handle(req):
        try:
            host=urlsplit('//'+req.host).hostname
            if not loopback(host or '') or req.headers.get('Origin'):
                raise GuardError('local_only','This service accepts local non-browser clients only.',403)
            if req.path=='/_guard/health':
                if req.method!='GET':raise GuardError('method_not_allowed','Use GET.',405)
                return web.json_response({'service':'CodexPic Guard','version':__version__,'config_hash':config.fingerprint,'ready':True,**store.status()})
            if req.path=='/_guard/shutdown':
                if req.method!='POST' or not hmac.compare_digest(req.headers.get('Authorization',''),'Bearer '+store.token_file.read_text(encoding='ascii')):
                    raise GuardError('not_authorized','Local admin authorization required.',403)
                app['shutdown_event'].set();return web.json_response({'stopping':True})
            prefix=config.local_prefix.rstrip('/')
            if not (req.path==prefix or req.path.startswith(prefix+'/')) or '..' in req.path.split('/'):
                raise GuardError('unknown_route','Path is outside the configured upstream route.',404)
            if req.headers.get('Upgrade','').lower()=='websocket':
                # Stock Codex recognizes 426 as HTTP fallback. Never silently forward unbudgeted WS images.
                return web.json_response({'error':{'code':'http_required','message':'CodexPic image budgets require HTTP requests.'}},status=426,headers={'Connection':'close'})
            if req.method not in ('GET','POST','DELETE'):raise GuardError('method_not_allowed','Unsupported proxy method.',405)
            async with app['slots']:
                return await forward(req)
        except GuardError as e:
            return web.json_response({'error':{'type':'codexpic_guard_error','code':e.code,'message':str(e)}},status=e.status)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({'error':{'code':'request_too_large','message':'Encoded request exceeds the configured local limit.'}},status=413)
        except (aiohttp.ClientError,TimeoutError,OSError):
            return web.json_response({'error':{'code':'upstream_unavailable','message':'The configured upstream/proxy failed. CodexPic did not retry through a direct connection.'}},status=502)
        except Exception:
            # No exception repr: parser errors may embed input, endpoints may contain secrets.
            return web.json_response({'error':{'code':'guard_internal_error','message':'Local request processing failed; no unbudgeted fallback was used.'}},status=500)

    async def forward(req):
        chunks=[];count=0
        async for chunk in req.content.iter_chunked(1024*1024):
            count+=len(chunk)
            if count>config.max_request_bytes:raise GuardError('request_too_large','Encoded request exceeds the local limit.',413)
            chunks.append(chunk)
        data=b''.join(chunks);del chunks
        encoded_before=len(data);encoding=req.headers.get('Content-Encoding','identity')
        stats={};scope='';rewritten=False
        is_responses=req.method=='POST' and req.path.rstrip('/').endswith(('/responses','/responses/compact'))
        if is_responses:
            raw=await asyncio.to_thread(unpack,data,encoding,config.max_request_bytes)
            try:body=json.loads(raw)
            except (ValueError,UnicodeError):raise GuardError('invalid_json','Responses request is not valid JSON.') from None
            del raw
            if not isinstance(body,dict):raise GuardError('invalid_json','Responses request must be an object.')
            body,scope,stats=await asyncio.to_thread(rewrite,body,req.headers,store,config.policy)
            rewritten=stats.get('changed',False)
            if rewritten:
                out=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode('utf-8')
                data=await asyncio.to_thread(pack,out,encoding)
        suffix=req.path[len(config.local_prefix.rstrip('/')):]
        target=config.upstream_base.rstrip('/')+suffix
        if req.query_string:target+='?'+req.query_string
        headers=filtered_headers(req.headers)
        if rewritten:
            for key in list(headers):
                if key.lower() in ('content-md5','digest','content-digest'):del headers[key]
        proxy=None if loopback(urlsplit(config.upstream_base).hostname or '') else config.upstream_proxy
        async with app['client'].request(req.method,target,data=data if req.method!='GET' else None,
                 headers=headers,proxy=proxy,allow_redirects=False) as upstream:
            if is_responses and 300<=upstream.status<400:
                raise GuardError('upstream_redirect_refused','A model endpoint redirected the request. Set its final upstream URL explicitly; CodexPic will not let the client replay an unbudgeted original body outside the guard.',502)
            response=web.StreamResponse(status=upstream.status,headers=filtered_headers(upstream.headers))
            await response.prepare(req)
            complete=False
            try:
                async for chunk in upstream.content.iter_chunked(64*1024):await response.write(chunk)
                await response.write_eof();complete=True
            except (aiohttp.ClientError,ConnectionError,TimeoutError):
                if req.transport: req.transport.close()
            finally:
                if is_responses:
                    store.record(scope,encoded_before,len(data),stats,upstream.status if complete else 0)
                    if complete and 200<=upstream.status<300:
                        store.consume_grants(scope,stats.get('original_grants',[]))
                        store.delivered(scope,stats.get('retained_ids',[]))
            return response
    app.router.add_route('*','/{path:.*}',handle)
    return app


async def run(config):
    app=make_app(config)
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    await web.TCPSite(runner,config.bind,config.port).start()
    try:await app['shutdown_event'].wait()
    finally:await runner.cleanup()
