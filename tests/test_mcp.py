import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from codexpic_guard.config import Config,Policy
from codexpic_guard.images import decode_url
from codexpic_guard.store import Store,scope_id
from tests.helpers import picture,image_block


class MCPTest(unittest.TestCase):
    def test_stdio_initialize_list_restore_and_pin(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);cfg=Config(data_dir=str(p/'cache'),upstream_base='http://127.0.0.1:1/v1')
            config=p/'guard.json';cfg.save(config);store=Store(cfg.data_dir,cfg.policy)
            raw=picture(44,(30,30));scope=scope_id('scope');ident,_=store.register(decode_url(image_block(raw)['image_url'],cfg.policy),scope)
            commands=[
                {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
                {'jsonrpc':'2.0','method':'notifications/initialized'},
                {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'codexpic_get_image','arguments':{'scope':scope,'image_id':ident,'original':True}}},
                {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'codexpic_pin_image','arguments':{'scope':scope,'image_id':ident,'pinned':True}}},
            ]
            proc=subprocess.run([sys.executable,'-m','codexpic_guard','mcp','--config',str(config),'--no-start'],input=''.join(json.dumps(x)+'\n' for x in commands),text=True,capture_output=True,timeout=15)
            self.assertEqual(proc.returncode,0,proc.stderr)
            replies=[json.loads(x) for x in proc.stdout.splitlines()]
            self.assertEqual(len(replies),4)
            self.assertEqual(replies[0]['result']['protocolVersion'],'2025-03-26')
            self.assertEqual(len(replies[1]['result']['tools']),4)
            self.assertEqual(base64.b64decode(replies[2]['result']['content'][1]['data']),raw)
            self.assertIn(ident,store.pinned(scope))
