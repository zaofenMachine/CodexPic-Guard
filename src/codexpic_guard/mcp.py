import base64
import json
import sys
from . import __version__
from .config import Config, GuardError
from .daemon import ensure_running
from .store import Store

COMMON={'scope':{'type':'string','description':'32-character scope copied exactly from the CodexPic placeholder.'},'image_id':{'type':'string','description':'Full CPIMG- identifier copied from the placeholder.'}}
TOOLS=[
 {'name':'codexpic_get_image','description':'Retrieve an image that CodexPic moved out of context. Use when a placeholder image is needed for visual judgment. Set original=true for seams, alpha edges or other fine detail; this costs more bandwidth. The latest returned image is protected by the request budget. Never guess details of an omitted image.',
  'inputSchema':{'type':'object','properties':{**COMMON,'original':{'type':'boolean','default':False}},'required':['scope','image_id'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'openWorldHint':False}},
 {'name':'codexpic_pin_image','description':'Keep a reference image in subsequent requests that contain it. Pinning preserves its presence, while normal preview compression still applies. Unpin finished references to save bandwidth.',
  'inputSchema':{'type':'object','properties':{**COMMON,'pinned':{'type':'boolean'}},'required':['scope','image_id','pinned'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False}},
 {'name':'codexpic_list_images','description':'List locally cached images in one CodexPic scope. Returns identifiers and dimensions, not image bytes.',
  'inputSchema':{'type':'object','properties':{'scope':COMMON['scope'],'limit':{'type':'integer','minimum':1,'maximum':100,'default':30}},'required':['scope'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'openWorldHint':False}},
 {'name':'codexpic_status','description':'Read local CodexPic request-size counters. These count encoded request bodies handled by the guard, not VPN billing or packet captures.',
  'inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True,'openWorldHint':False}},
]


def call(store,name,args):
    if not isinstance(args,dict):raise GuardError('invalid_arguments','Tool arguments must be an object.')
    allowed=next((t['inputSchema'] for t in TOOLS if t['name']==name),None)
    if allowed is None:raise GuardError('unknown_tool','Unknown tool.')
    if set(args)-set(allowed['properties']) or any(k not in args for k in allowed.get('required',[])):
        raise GuardError('invalid_arguments','Missing or unexpected tool arguments.')
    if name=='codexpic_get_image':
        original=args.get('original',False)
        if not isinstance(original,bool):raise GuardError('invalid_arguments','original must be a boolean.')
        image=store.get(args['scope'],args['image_id'],original=original)
        if original:store.grant_original(args['scope'],args['image_id'])
        return {'content':[{'type':'text','text':f"CodexPic {args['image_id']} scope={args['scope']}; {'original' if original else 'preview'} {image.width}x{image.height}; {len(image.data)} bytes."},
                           {'type':'image','data':base64.b64encode(image.data).decode('ascii'),'mimeType':image.mime,
                            **({'_meta':{'codex/imageDetail':'original'}} if original else {})}]}
    if name=='codexpic_pin_image':
        if not isinstance(args['pinned'],bool):raise GuardError('invalid_arguments','pinned must be a boolean.')
        store.pin(args['scope'],args['image_id'],args['pinned']);value={'pinned':args['pinned'],'image_id':args['image_id']}
    elif name=='codexpic_list_images':
        limit=args.get('limit',30)
        if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=100:raise GuardError('invalid_arguments','limit must be 1..100.')
        value=store.list_images(args['scope'],limit)
    else:value=store.status()
    return {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}


def serve(config_path,start_proxy=True):
    config=Config.load(config_path)
    if start_proxy:ensure_running(config_path)
    store=Store(config.data_dir,config.policy)
    for raw in sys.stdin.buffer:
        if len(raw)>1_048_576:continue
        req=None
        try:
            req=json.loads(raw);method=req.get('method');params=req.get('params') or {}
            if 'id' not in req:continue
            if method=='initialize':
                requested=params.get('protocolVersion','2024-11-05')
                version=requested if requested in ('2024-11-05','2025-03-26','2025-06-18') else '2025-06-18'
                result={'protocolVersion':version,'capabilities':{'tools':{}},'serverInfo':{'name':'codexpic-guard','version':__version__},'instructions':'Retrieve original images when a CodexPic placeholder is relevant to fine visual judgments.'}
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='tools/call':result=call(store,params.get('name'),params.get('arguments') or {})
            elif method=='ping':result={}
            else:
                reply={'jsonrpc':'2.0','id':req['id'],'error':{'code':-32601,'message':'Method not found'}}
                sys.stdout.write(json.dumps(reply)+'\n');sys.stdout.flush();continue
            reply={'jsonrpc':'2.0','id':req['id'],'result':result}
        except GuardError as e:
            reply={'jsonrpc':'2.0','id':req.get('id') if isinstance(req,dict) else None,'result':{'isError':True,'content':[{'type':'text','text':e.code+': '+str(e)}]}}
        except Exception:
            reply={'jsonrpc':'2.0','id':req.get('id') if isinstance(req,dict) else None,'error':{'code':-32602,'message':'Invalid request or local operation failed'}}
        sys.stdout.write(json.dumps(reply,ensure_ascii=False)+'\n');sys.stdout.flush()
