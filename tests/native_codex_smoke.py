"""Run the installed Codex against local mock Responses and the real guard/MCP.

Uses an isolated CODEX_HOME, no credentials, no external model endpoint, and
synthetic images. Does not launch or restart the desktop app.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from aiohttp import web
from codexpic_guard.config import Config
from codexpic_guard.proxy import make_app,unpack
from codexpic_guard.rewrite import refs_in
from codexpic_guard.install import enable,disable
from tests.helpers import picture
from tests.test_proxy import listen


def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def advertised_tools(tools,namespace=None):
    for tool in tools:
        if tool.get('type')=='namespace':yield from advertised_tools(tool.get('tools',[]),tool.get('name'))
        elif tool.get('name'):yield namespace,tool['name']


async def run(binary,root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    home=root/'home';home.mkdir(exist_ok=True);work=root/'work';work.mkdir(exist_ok=True)
    fixture=work/'synthetic-frame.png';fixture.write_bytes(picture(817,(1000,700)))
    calls=[];guard=None;scope=None;image_id=None;requested=False
    async def backend(req):
        nonlocal scope,image_id,requested
        if req.method=='GET':return web.json_response({'models':[]})
        wire=await req.read();raw=unpack(wire,req.headers.get('Content-Encoding'),64*1024*1024);body=json.loads(raw)
        images=refs_in(body)
        all_tools=list(body.get('tools',[]))
        for item in body.get('input',[]):
            if isinstance(item,dict) and item.get('type')=='additional_tools':all_tools.extend(item.get('tools',[]))
        tools=list(advertised_tools(all_tools))
        entry={'request_bytes':len(raw),'wire_bytes':len(wire),'encoding':req.headers.get('Content-Encoding'),'image_bytes':[],
               'body_keys':list(body.keys()),'tool_shapes':[{k:(v if isinstance(v,str) else list(v.keys()) if isinstance(v,dict) else type(v).__name__) for k,v in t.items()} for t in all_tools],
               'identity_header_names':[k for k in req.headers if any(x in k.lower() for x in ('session','conversation','thread'))],
               'tool_names':[('.'.join(x for x in pair if x)) for pair in tools]}
        for ref in images:
            data=base64.b64decode(ref.block['image_url'].split(',',1)[1]);entry['image_bytes'].append(len(data))
        entry['input_types']=[(x.get('type'),list(x.keys())) for x in body.get('input',[]) if isinstance(x,dict)]
        entry['tool_declaration_snippets']=[]
        for x in body.get('input',[]):
            if not isinstance(x,dict):continue
            for content in x.get('content',[]) if isinstance(x.get('content'),list) else []:
                text=content.get('text','')
                if 'codexpic_get_image' in text:
                    at=text.find('codexpic_get_image');entry['tool_declaration_snippets'].append(text[max(0,at-200):at+450])
        calls.append(entry)
        if len(calls)>4:return web.json_response({'error':{'message':'Mock request limit reached'}},status=400)
        choices=[x for x in tools if x[1].endswith('codexpic_get_image')]
        if not choices and entry['tool_declaration_snippets']:choices=[('mcp__codexpic','codexpic_get_image')]
        if (choices or ('functions','exec') in tools) and not requested:
            with guard['store'].connect() as c:
                row=c.execute('SELECT scope,id FROM scoped_images ORDER BY last_seen DESC LIMIT 1').fetchone()
            scope,image_id=row['scope'],row['id'];requested=True
            arguments={'scope':scope,'image_id':image_id,'original':True}
            if choices:
                namespace,name=choices[0]
                item={'type':'function_call','id':'fc_codexpic_smoke','call_id':'call_codexpic_smoke','name':name,'arguments':json.dumps(arguments)}
                if namespace:item['namespace']=namespace
            else:
                code=('const t=ALL_TOOLS.find(x=>x.name.endsWith("codexpic_get_image")); '
                      'if(!t) throw new Error("CodexPic MCP tool missing"); '
                      f'const r=await tools[t.name]({json.dumps(arguments)}); '
                      'for(const c of (r.content??[])){if(c.type==="image") image(c); else if(c.type==="text") text(c.text);}')
                item={'type':'custom_tool_call','id':'ctc_codexpic_smoke','call_id':'call_codexpic_smoke','name':'exec','namespace':'functions','input':code}
        else:
            item={'type':'message','id':'msg_codexpic_smoke','role':'assistant','status':'completed','phase':'final_answer',
                  'content':[{'type':'output_text','text':'CODEXPIC_MOCK_OK','annotations':[]}]}
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await response.prepare(req)
        resp={'id':f'resp_codexpic_{len(calls)}','object':'response','created_at':int(time.time()),'model':body.get('model'),
              'status':'in_progress','output':[]}
        events=[('response.created',{'type':'response.created','response':resp}),
                ('response.output_item.done',{'type':'response.output_item.done','output_index':0,'item':item}),
                ('response.completed',{'type':'response.completed','response':{**resp,'status':'completed','output':[item],
                    'usage':{'input_tokens':100,'output_tokens':10,'total_tokens':110,'input_tokens_details':{'cached_tokens':0}}}})]
        for name,data in events:await response.write(f'event: {name}\ndata: {json.dumps(data)}\n\n'.encode())
        await response.write_eof();return response
    upstream=web.Application(client_max_size=64*1024*1024,handler_args={'auto_decompress':False})
    upstream.router.add_route('*','/{path:.*}',backend);up_runner,up_port=await listen(upstream)
    port=free_port();cfg=Config(data_dir=str(root/'cache'),upstream_base=f'http://127.0.0.1:{up_port}/v1',local_prefix='/v1',port=port)
    config=root/'guard.json';cfg.save(config);guard=make_app(cfg)
    runner=web.AppRunner(guard,access_log=None);await runner.setup();await web.TCPSite(runner,'127.0.0.1',port).start()
    toml=(f'model = "gpt-6-astra"\nmodel_provider = "codexpic_mock"\napproval_policy = "never"\n'
          f'[model_providers.codexpic_mock]\nname="Local CodexPic mock"\nbase_url={json.dumps(cfg.upstream_base)}\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=true\n'
          f'[analytics]\nenabled=false\n[features]\ncode_mode=false\n')
    (home/'config.toml').write_text(toml,encoding='utf-8')
    enable(home,config,start=False)
    env=dict(os.environ);env['CODEX_HOME']=str(home);env['OPENAI_API_KEY']='mock-no-real-key'
    env.pop('NO_PROXY',None);env.pop('no_proxy',None)
    for key in ('OPENAI_BASE_URL','CODEX_API_KEY'):env.pop(key,None)
    args=[str(binary),'exec','--strict-config','--skip-git-repo-check','--ephemeral','--json','-C',str(work),'-s','read-only','-i',str(fixture),'--','Return CODEXPIC_MOCK_OK. This is a local transport test.']
    try:
        proc=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,
                                                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        try:stdout,stderr=await asyncio.wait_for(proc.communicate(),90)
        except asyncio.TimeoutError:proc.kill();stdout,stderr=await proc.communicate();raise RuntimeError('Native Codex smoke timed out')
        (root/'codex.stdout.jsonl').write_bytes(stdout);(root/'codex.stderr.txt').write_bytes(stderr)
        original_size=0
        if image_id:original_size=len(guard['store'].get(scope,image_id,original=True).data)
        result={'exit_code':proc.returncode,'model_endpoint':'local mock only','no_proxy_source':'installer-written isolated .env; no test environment override','requests':calls,'mcp_original_requested':requested,
                'original_cached_bytes':original_size,'restored_original_observed':len(calls)>1 and original_size in calls[-1]['image_bytes'],
                'success_marker':'CODEXPIC_MOCK_OK' in stdout.decode('utf-8','replace'),'guard_metrics':guard['store'].status()}
        result['passed']=proc.returncode==0 and result['success_marker'] and requested and result['restored_original_observed'] and calls[0]['image_bytes'][0]<=512000
        (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='requests'},indent=2))
        if not result['passed']:print(stderr.decode('utf-8','replace')[-2200:]);raise SystemExit(1)
    finally:
        disable(config)
        await runner.cleanup();await up_runner.cleanup()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--codex',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    asyncio.run(run(a.codex,a.out))
