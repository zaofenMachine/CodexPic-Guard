from pathlib import Path
import socket
import tempfile
import time
import unittest
from codexpic_guard.config import Config
from codexpic_guard.daemon import ensure_running,health,stop


class DaemonTest(unittest.TestCase):
    def test_hidden_service_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            config=Config(data_dir=str(Path(d)/'cache'),upstream_base='http://127.0.0.1:1/v1',port=port)
            path=Path(d)/'guard.json';config.save(path)
            try:
                status=ensure_running(path);self.assertEqual(status['config_hash'],config.fingerprint)
                self.assertTrue(ensure_running(path)['ready'])
            finally:
                if health(config):stop(path)
                for _ in range(50):
                    if not health(config,.1):break
                    time.sleep(.05)
            self.assertIsNone(health(config,.1))
