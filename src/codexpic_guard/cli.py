import argparse
import asyncio
import json
from pathlib import Path
import sys
from . import __version__
from .config import Config, GuardError
from .daemon import ensure_running, health, stop
from .install import codex_home, default_data, prepare, enable, disable, installation_status
from .store import Store


def main():
    parser=argparse.ArgumentParser(prog='codexpic-guard',description='Local image budgets and reversible offload for Codex.')
    parser.add_argument('--version',action='version',version=__version__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare',help='Create a guard config; does not modify Codex or start a service.')
    p.add_argument('--codex-home',type=Path,default=codex_home());p.add_argument('--data-dir',type=Path,default=default_data())
    p.add_argument('--proxy');p.add_argument('--direct',action='store_true');p.add_argument('--upstream');p.add_argument('--port',type=int,default=17993)
    for name in ('serve','start','status','enable','disable','stop','mcp'):
        p=sub.add_parser(name);p.add_argument('--config',type=Path,default=default_data()/'guard.json')
        if name=='enable':p.add_argument('--codex-home',type=Path,default=codex_home())
        if name=='stop':p.add_argument('--force',action='store_true')
        if name=='mcp':p.add_argument('--no-start',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:
        if args.command=='prepare':
            path,cfg,_=prepare(args.codex_home,args.data_dir,args.proxy,args.direct,args.upstream,args.port)
            result={'prepared':str(path),'local_base':cfg.local_base,'codex_changed':False}
        elif args.command=='serve':
            from .proxy import run
            asyncio.run(run(Config.load(args.config)));return
        elif args.command=='mcp':
            from .mcp import serve
            serve(args.config,start_proxy=not args.no_start);return
        elif args.command=='start':result=ensure_running(args.config)
        elif args.command=='enable':result=enable(args.codex_home,args.config)
        elif args.command=='disable':result=disable(args.config)
        elif args.command=='stop':
            cfg=Config.load(args.config);state=Path(cfg.data_dir)/'installation.json'
            if state.exists() and json.loads(state.read_text(encoding='utf-8')).get('enabled') and not args.force:
                raise GuardError('still_enabled','Disable and restart Codex before stopping the proxy, or use --force explicitly.')
            result=stop(args.config)
        else:
            cfg=Config.load(args.config);state=Path(cfg.data_dir)/'installation.json'
            status=installation_status(args.config)
            result={'running':health(cfg),'enabled_for_next_start':status['enabled_requested'] and status['configuration_matches'],**status,'configuration':str(args.config)}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (GuardError,ValueError,OSError) as e:
        print(json.dumps({'error':getattr(e,'code',type(e).__name__),'message':str(e)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)
