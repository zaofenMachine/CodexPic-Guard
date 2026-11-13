import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codexpic_guard.config import Policy
from experiments.live_transport import Observation, cli_usage, preview_request
from tests.helpers import image_block, picture


class ExperimentTest(unittest.TestCase):
    def test_sse_usage_without_space_across_chunks(self):
        obs = Observation()
        event = {'type': 'response.completed', 'response': {'status': 'completed',
                 'usage': {'input_tokens': 40, 'attribution': {'private_id': {}}}}}
        raw = ('event:response.completed\r\ndata:' + json.dumps(event) + '\r\n\r\n').encode()
        for at in range(0, len(raw), 7):
            obs.sse(raw[at:at + 7])
        self.assertEqual(obs.responses[0]['usage'], {'input_tokens': 40})

    def test_missing_usage_is_not_reported_as_zero(self):
        self.assertIsNone(cli_usage(b'{"type":"turn.failed"}\n'))

    def test_cli_usage_keeps_cache_writes_separate(self):
        raw = json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 100, 'cached_input_tokens': 60, 'cache_write_input_tokens': 20,
            'output_tokens': 5}}).encode()
        self.assertEqual(cli_usage(raw)['uncached_input_tokens'], 40)
        self.assertEqual(cli_usage(raw)['cache_write_input_tokens'], 20)

    def test_preview_preserves_lineage_and_non_image_fields(self):
        image = image_block(picture(12, (400, 300)))
        body = {'type': 'response.create', 'previous_response_id': 'resp_old',
                'generate': True, 'input': [{'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'unchanged'}, image]}]}
        policy = Policy(image_target_bytes=40000); cache = {}
        original = copy.deepcopy(body)
        self.assertEqual(preview_request(body, cache, policy), 1)
        self.assertEqual(body['previous_response_id'], 'resp_old')
        self.assertEqual(body['input'][0]['content'][0], original['input'][0]['content'][0])
        again = copy.deepcopy(original); preview_request(again, cache, policy)
        self.assertEqual(again, body)
        delta = {'previous_response_id': 'resp_next', 'input': []}
        self.assertEqual(preview_request(delta, cache, policy), 0)
        self.assertEqual(delta, {'previous_response_id': 'resp_next', 'input': []})

    def test_fixture_mcp_starts_from_unrelated_working_directory(self):
        script = Path(__file__).resolve().parents[1] / 'experiments/live_transport.py'
        with tempfile.TemporaryDirectory() as d:
            lines = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                     {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}]
            result = subprocess.run([sys.executable, str(script), 'mcp', d], cwd=d,
                input='\n'.join(json.dumps(x) for x in lines) + '\n', text=True,
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(x) for x in result.stdout.splitlines()]
            self.assertTrue(replies[1]['result']['tools'][0]['annotations']['readOnlyHint'])
