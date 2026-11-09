import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit
import tomlkit
from .config import Config, GuardError
from .daemon import ensure_running

MISSING={'__codexpic_missing__':True}
ENV_BEGIN='# BEGIN CodexPic Guard loopback'
ENV_END='# END CodexPic Guard loopback'


def codex_home():return Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))).resolve()
def default_data():return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'CodexPicGuard'


def atomic_write(path,text):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_name(p.name+'.codexpic-tmp-'+str(os.getpid()))
    try:
        with temp.open('w',encoding='utf-8',newline='') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(temp,p)
    finally:
        if temp.exists():temp.unlink()


def lookup(doc,path):
    x=doc
    for k in path:
        if k not in x:return MISSING
        x=x[k]
    return x.unwrap() if hasattr(x,'unwrap') else x


def assign(doc,path,value):
    x=doc
    for k in path[:-1]:
        if k not in x:x[k]=tomlkit.table()
        x=x[k]
    if value==MISSING:
        x.pop(path[-1],None)
        # Keep pre-existing table layout and comments. An empty table is harmless.
    else:x[path[-1]]=value


def discover_proxy():
    for name in ('HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy'):
        if os.environ.get(name):return os.environ[name]
    if os.name=='nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as k:
                if not winreg.QueryValueEx(k,'ProxyEnable')[0]:return None
                value=winreg.QueryValueEx(k,'ProxyServer')[0]
                if '=' in value:
                    d=dict(x.split('=',1) for x in value.split(';') if '=' in x);value=d.get('https') or d.get('http')
                return value if '://' in value else 'http://'+value
        except (OSError,TypeError):return None
    return None


def prepare(home,data_dir,proxy=None,direct=False,upstream=None,port=17993):
    home=Path(home).resolve();data=Path(data_dir).resolve()
    text=(home/'config.toml').read_text(encoding='utf-8-sig') if (home/'config.toml').exists() else ''
    doc=tomlkit.parse(text);provider=doc.get('model_provider','openai')
    if doc.get('profile'):raise GuardError('profile_not_managed','A selected external Codex profile needs separate routing review; the base config was not changed.')
    if provider!='openai' and doc.get('model_providers',{}).get(provider,{}).get('wire_api','responses')!='responses':
        raise GuardError('unsupported_wire_api','Only native Responses providers are supported.')
    route=['openai_base_url'] if provider=='openai' else ['model_providers',provider,'base_url']
    previous=lookup(doc,route)
    endpoint=upstream or (previous if previous!=MISSING else 'https://chatgpt.com/backend-api/codex' if provider=='openai' else None)
    if endpoint is None:raise GuardError('missing_upstream','Custom provider has no base_url; specify the upstream explicitly.')
    if loop_address(endpoint,port):raise GuardError('already_routed','Configuration already points at this local port. Use status or disable first.')
    config=Config(data_dir=str(data),upstream_base=endpoint,upstream_proxy=proxy or discover_proxy(),allow_direct=direct,port=port)
    config.validate();path=data/'guard.json'
    if (data/'installation.json').exists():
        state=json.loads((data/'installation.json').read_text(encoding='utf-8'))
        if state.get('enabled'):raise GuardError('already_enabled','Guard is already enabled. Use status before changing the configuration.')
    config.save(path)
    return path,config,route


def loop_address(url,port):
    u=urlsplit(url);return u.hostname in ('127.0.0.1','localhost','::1') and u.port==port


def env_plan(text):
    if ENV_BEGIN in text or ENV_END in text:raise GuardError('env_conflict','An existing CodexPic .env block must be removed with disable first.')
    lines=text.splitlines(keepends=True)
    matches=[(i,x) for i,x in enumerate(lines) if re.match(r'\s*(?:export\s+)?NO_PROXY\s*=',x,re.I)]
    if len(matches)>1:raise GuardError('env_conflict','Multiple NO_PROXY assignments need manual consolidation.')
    old_line=matches[0][1] if matches else None
    value=old_line.split('=',1)[1].strip().strip('"\'') if old_line else os.environ.get('NO_PROXY',os.environ.get('no_proxy',''))
    if any(x in value for x in ('\n','\r','"',"'",'#','$')):raise GuardError('env_conflict','Complex NO_PROXY syntax is not edited automatically.')
    entries=list(dict.fromkeys([x.strip() for x in value.split(',') if x.strip()]+['127.0.0.1','localhost','::1']))
    block=ENV_BEGIN+'\nNO_PROXY='+','.join(entries)+'\n'+ENV_END+'\n'
    if matches:
        index=matches[0][0];lines[index]=block;new=''.join(lines)
    else:new=text+('' if not text or text.endswith('\n') else '\n')+block
    return new,{'block':block,'old_line':old_line}


def enable(home,config_path,start=True):
    home=Path(home).resolve();config=Config.load(config_path);data=Path(config.data_dir)
    state_path=data/'installation.json'
    if state_path.exists() and json.loads(state_path.read_text(encoding='utf-8')).get('enabled'):
        if not installation_status(config_path)['configuration_matches']:
            raise GuardError('config_conflict','Managed settings or the active provider changed; review or disable the previous installation first.',409)
        return {'enabled':True,'already_enabled':True,'restart_required':True}
    cfg=home/'config.toml';env=home/'.env'
    before=cfg.read_text(encoding='utf-8-sig') if cfg.exists() else ''
    env_before=env.read_text(encoding='utf-8') if env.exists() else ''
    doc=tomlkit.parse(before);provider=doc.get('model_provider','openai')
    if doc.get('profile'):raise GuardError('profile_not_managed','External profile routing is not automatically rewritten.')
    if provider!='openai' and doc.get('model_providers',{}).get(provider,{}).get('wire_api','responses')!='responses':
        raise GuardError('unsupported_wire_api','Only native Responses providers are supported.')
    route=['openai_base_url'] if provider=='openai' else ['model_providers',provider,'base_url']
    existing=lookup(doc,route)
    if existing!=MISSING and existing!=config.upstream_base:
        raise GuardError('route_changed','Upstream configuration changed since prepare; rerun prepare.')
    if lookup(doc,['mcp_servers','codexpic'])!=MISSING:
        raise GuardError('mcp_name_conflict','mcp_servers.codexpic already exists and is not managed by this installation.')
    if lookup(doc,['features','respect_system_proxy']) is False:
        raise GuardError('proxy_feature_conflict','Codex explicitly disables respect_system_proxy. Review that preference before enabling this Windows proxy integration.')
    edits=[(route,config.local_base),(['features','respect_system_proxy'],True),
           (['mcp_servers','codexpic'],{'command':sys.executable,'args':['-m','codexpic_guard','mcp','--config',str(Path(config_path).resolve())],'startup_timeout_sec':30,'tool_timeout_sec':60,'required':True})]
    changes=[]
    for path,value in edits:
        changes.append({'path':path,'before':copy.deepcopy(lookup(doc,path)),'after':value});assign(doc,path,value)
    env_after,env_change=env_plan(env_before)
    after=tomlkit.dumps(doc);tomlkit.parse(after)
    if start:ensure_running(config_path)
    backups=data/'backups';backups.mkdir(parents=True,exist_ok=True)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if cfg.exists():shutil.copy2(cfg,backups/(stamp+'-config.toml'))
    if env.exists():shutil.copy2(env,backups/(stamp+'-.env'))
    state={'enabled':True,'provider':provider,'codex_home':str(home),'config_path':str(Path(config_path).resolve()),'changes':changes,'env_change':env_change,'env_existed':env.exists(),'config_existed':cfg.exists(),'before_sha256':hashlib.sha256(before.encode()).hexdigest()}
    # Optimistic concurrency guard immediately before writing either configuration file.
    if (cfg.read_text(encoding='utf-8-sig') if cfg.exists() else '')!=before or (env.read_text(encoding='utf-8') if env.exists() else '')!=env_before:
        raise GuardError('concurrent_config_edit','Codex configuration changed during setup; nothing was overwritten.')
    atomic_write(state_path,json.dumps(state,indent=2))
    try:
        atomic_write(cfg,after);atomic_write(env,env_after)
    except Exception:
        atomic_write(cfg,before);atomic_write(env,env_before)
        state['enabled']=False;atomic_write(state_path,json.dumps(state,indent=2));raise
    return {'enabled':True,'restart_required':True,'local_base':config.local_base,'data_dir':str(data)}


def disable(config_path):
    config=Config.load(config_path);state_path=Path(config.data_dir)/'installation.json'
    if not state_path.exists():return {'enabled':False,'already_disabled':True}
    state=json.loads(state_path.read_text(encoding='utf-8'))
    if not state.get('enabled'):return {'enabled':False,'already_disabled':True}
    home=Path(state['codex_home']);cfg=home/'config.toml';env=home/'.env'
    before=cfg.read_text(encoding='utf-8-sig');env_before=env.read_text(encoding='utf-8') if env.exists() else ''
    doc=tomlkit.parse(before)
    for change in state['changes']:
        if lookup(doc,change['path'])!=change['after']:
            raise GuardError('config_conflict','A managed configuration value was changed externally. No settings were restored; inspect the saved installation state.',409)
    ec=state['env_change']
    if env_before.count(ec['block'])!=1:
        raise GuardError('env_conflict','The managed NO_PROXY block changed externally. No settings were restored.',409)
    for change in reversed(state['changes']):assign(doc,change['path'],change['before'])
    after=tomlkit.dumps(doc);env_after=env_before.replace(ec['block'],ec['old_line'] or '',1)
    if cfg.read_text(encoding='utf-8-sig')!=before or (env.read_text(encoding='utf-8') if env.exists() else '')!=env_before:
        raise GuardError('concurrent_config_edit','Configuration changed during restore; nothing was overwritten.')
    atomic_write(cfg,after);atomic_write(env,env_after)
    state['enabled']=False;atomic_write(state_path,json.dumps(state,indent=2))
    return {'enabled':False,'restart_required':True,'proxy_kept_running_for_existing_sessions':True}


def installation_status(config_path):
    config=Config.load(config_path);p=Path(config.data_dir)/'installation.json'
    if not p.exists():return {'enabled_requested':False,'configuration_matches':False,'conflicts':[]}
    state=json.loads(p.read_text(encoding='utf-8'))
    if not state.get('enabled'):return {'enabled_requested':False,'configuration_matches':False,'conflicts':[]}
    home=Path(state['codex_home']);doc=tomlkit.parse((home/'config.toml').read_text(encoding='utf-8-sig'));conflicts=[]
    for change in state['changes']:
        if lookup(doc,change['path'])!=change['after']:conflicts.append('.'.join(change['path']))
    if doc.get('model_provider','openai')!=state.get('provider','openai'):conflicts.append('active model_provider')
    env=(home/'.env').read_text(encoding='utf-8') if (home/'.env').exists() else ''
    if state['env_change']['block'] not in env:conflicts.append('managed NO_PROXY block')
    return {'enabled_requested':True,'configuration_matches':not conflicts,'conflicts':conflicts}
