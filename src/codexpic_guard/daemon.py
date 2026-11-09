import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from .config import Config, GuardError


def health(config,timeout=1):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://{config.bind}:{config.port}/_guard/health',timeout=timeout) as r:
            d=json.load(r)
            return d if d.get('service')=='CodexPic Guard' else None
    except Exception:return None


def ensure_running(config_path):
    config=Config.load(config_path)
    current=health(config)
    if current:
        if current.get('config_hash')!=config.fingerprint:raise GuardError('config_mismatch','A different CodexPic configuration is already listening on this port. Stop that guard explicitly before changing it.',409)
        return current
    kwargs={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
    else:kwargs['start_new_session']=True
    process=subprocess.Popen([sys.executable,'-m','codexpic_guard','serve','--config',str(Path(config_path).resolve())],**kwargs)
    for _ in range(80):
        current=health(config,0.3)
        if current:
            if current.get('config_hash')!=config.fingerprint:raise GuardError('config_mismatch','A different CodexPic configuration uses this port.',409)
            return current
        if process.poll() is not None:break
        time.sleep(0.1)
    raise GuardError('startup_failed','CodexPic proxy failed to start. Check the configured port with the status command.',503)


def stop(config_path):
    config=Config.load(config_path)
    token=(Path(config.data_dir)/'admin-token').read_text(encoding='ascii')
    req=urllib.request.Request(f'http://{config.bind}:{config.port}/_guard/shutdown',data=b'',method='POST',headers={'Authorization':'Bearer '+token})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as r:return json.load(r)
