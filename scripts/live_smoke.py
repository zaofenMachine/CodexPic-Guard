"""Opt-in live smoke test: one synthetic image, existing Codex ChatGPT login.

Consumes account usage. Requires --run. Never copies authentication files or
changes global configuration. At most three model requests, 120-second timeout.
"""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import time
from urllib.parse import urlsplit
from aiohttp import web
from PIL import Image,ImageDraw
from codexpic_guard.config import Config
from codexpic_guard.install import default_data,codex_home
from codexpic_guard.proxy import make_app


async def run(binary,root,report):
    login=await asyncio.create_subprocess_exec(str(binary),'login','status',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    login_out,login_err=await login.communicate()
    if login.returncode!=0 or b'ChatGPT' not in login_out+login_err:
        raise SystemExit('This opt-in test requires an existing ChatGPT login; it will not use API-key billing.')
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True);work=root/'work';work.mkdir(exist_ok=True)
    rng=random.Random(8801);pixels=bytes(205+rng.randrange(40) for _ in range(1200*800*3))
    im=Image.frombytes('RGB',(1200,800),pixels);draw=ImageDraw.Draw(im)
    draw.ellipse((120,220,470,570),fill=(230,20,20));draw.rectangle((730,220,1080,570),fill=(20,40,230))
    image=work/'synthetic-shapes.png';im.save(image)
    schema=work/'schema.json';schema.write_text(json.dumps({'type':'object','properties':{'red':{'type':'string'},'blue':{'type':'string'}},'required':['red','blue'],'additionalProperties':False}))
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    cfg=replace(Config.load(default_data()/'guard.json'),port=port,data_dir=str(root/'cache'),upstream_timeout_seconds=90)
    if urlsplit(cfg.upstream_base).hostname!='chatgpt.com' or urlsplit(cfg.upstream_base).path.rstrip('/')!='/backend-api/codex':
        raise SystemExit('This test only supports the official ChatGPT Codex route; no credentials will be forwarded to a custom endpoint.')
    app=make_app(cfg);attempts=0
    @web.middleware
    async def limit(req,handler):
        nonlocal attempts
        if req.method=='POST' and req.path.endswith('/responses'):
            attempts+=1
            if attempts>3:return web.json_response({'error':{'message':'Live smoke request limit reached'}},status=429)
        return await handler(req)
    app.middlewares.append(limit);runner=web.AppRunner(app,access_log=None);await runner.setup();await web.TCPSite(runner,'127.0.0.1',port).start()
    paths=[codex_home()/'config.toml',codex_home()/'.env']
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}
    env=dict(os.environ);env['NO_PROXY']='127.0.0.1,localhost,::1'
    # Authentication stays with Codex's normal credential handling. No keys are read by this script.
    args=[str(binary),'exec','--ignore-user-config','--skip-git-repo-check','--ephemeral','--json','-C',str(work),'-s','read-only','-m','gpt-6-astra',
          '-c','approval_policy="never"','-c','model_reasoning_effort="low"','-c','analytics.enabled=false',
          '-c','features.respect_system_proxy=true','-c','openai_base_url='+json.dumps(cfg.local_base),
          '--output-schema',str(schema),'-o',str(root/'answer.json'),'-i',str(image),'--',
          'Inspect the attached synthetic image. What shape is filled red, and what shape is filled blue? Return JSON with red and blue, using English shape names. No tools are needed.']
    started=time.perf_counter();result={'mode':'live service, synthetic shapes only','model':'gpt-6-astra','credential_files_copied':False,'global_routing_changed':False}
    try:
        proc=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        try:out,err=await asyncio.wait_for(proc.communicate(),120)
        except asyncio.TimeoutError:
            proc.kill();out,err=await proc.communicate();result['timed_out']=True
        (root/'stdout.jsonl').write_bytes(out);(root/'stderr.txt').write_bytes(err)
        answer=None
        try:answer=json.loads((root/'answer.json').read_text(encoding='utf-8'))
        except Exception:pass
        metrics=app['store'].status()
        after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}
        result.update(exit_code=proc.returncode,requests=attempts,seconds=round(time.perf_counter()-started,3),answer=answer,metrics=metrics,global_config_hashes_unchanged=before==after)
        result['passed']=proc.returncode==0 and answer is not None and answer.get('red','').lower()=='circle' and answer.get('blue','').lower()=='square' and metrics['requests']>=1 and before==after
    finally:await runner.cleanup()
    report=Path(report);report.parent.mkdir(exist_ok=True,parents=True);report.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true',required=True);p.add_argument('--codex',required=True);p.add_argument('--out',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    asyncio.run(run(a.codex,a.out,a.report))
