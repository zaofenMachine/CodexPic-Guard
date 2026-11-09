import json
from pathlib import Path
import tempfile
import unittest
import tomlkit
from codexpic_guard.config import Config,GuardError
from codexpic_guard.install import enable,disable,installation_status


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name);self.home=self.root/'codex';self.home.mkdir()
        self.data=self.root/'guard';self.cfg=self.data/'guard.json'
        Config(data_dir=str(self.data),upstream_proxy='http://127.0.0.1:7897').save(self.cfg)
        self.original='# user comment\nmodel = "gpt-6-astra"\n[features]\ncode_mode = true\n[mcp_servers.existing]\ncommand = "keep-me"\n'
        (self.home/'config.toml').write_text(self.original,encoding='utf-8')

    def test_enable_disable_preserves_other_settings_and_comments(self):
        (self.home/'.env').write_text('SOME_PRIVATE_VALUE=secret-fixture\nNO_PROXY=internal.example\n',encoding='utf-8')
        enable(self.home,self.cfg,start=False)
        doc=tomlkit.parse((self.home/'config.toml').read_text())
        self.assertEqual(doc['model'],'gpt-6-astra');self.assertEqual(doc['mcp_servers']['existing']['command'],'keep-me')
        self.assertIn('127.0.0.1',(self.home/'.env').read_text())
        self.assertNotIn('secret-fixture',(self.data/'installation.json').read_text())
        # An independent user edit must survive disable.
        doc['model_verbosity']='low';(self.home/'config.toml').write_text(tomlkit.dumps(doc),encoding='utf-8')
        disable(self.cfg);after=(self.home/'config.toml').read_text();d=tomlkit.parse(after)
        self.assertIn('# user comment',after);self.assertEqual(d['model_verbosity'],'low')
        self.assertNotIn('openai_base_url',d);self.assertNotIn('codexpic',d['mcp_servers'])
        self.assertEqual((self.home/'.env').read_text(),'SOME_PRIVATE_VALUE=secret-fixture\nNO_PROXY=internal.example\n')

    def test_conflicting_external_edit_is_not_overwritten(self):
        enable(self.home,self.cfg,start=False)
        doc=tomlkit.parse((self.home/'config.toml').read_text());doc['openai_base_url']='https://different.invalid/v1'
        edited=tomlkit.dumps(doc);(self.home/'config.toml').write_text(edited)
        with self.assertRaises(GuardError):disable(self.cfg)
        self.assertEqual((self.home/'config.toml').read_text(),edited)
        self.assertFalse(installation_status(self.cfg)['configuration_matches'])
        with self.assertRaises(GuardError):enable(self.home,self.cfg,start=False)

    def test_idempotence(self):
        enable(self.home,self.cfg,start=False);self.assertTrue(enable(self.home,self.cfg,start=False)['already_enabled'])
        disable(self.cfg);self.assertTrue(disable(self.cfg)['already_disabled'])

    def test_custom_provider_preserves_auth_and_model(self):
        text='model_provider="mine"\n[model_providers.mine]\nname="Mine"\nbase_url="https://gateway.invalid/v1"\nenv_key="PRIVATE_KEY_ENV"\nwire_api="responses"\n'
        (self.home/'config.toml').write_text(text)
        Config(data_dir=str(self.data),upstream_base='https://gateway.invalid/v1',upstream_proxy='http://127.0.0.1:7897').save(self.cfg)
        enable(self.home,self.cfg,start=False);doc=tomlkit.parse((self.home/'config.toml').read_text())
        self.assertEqual(doc['model_providers']['mine']['env_key'],'PRIVATE_KEY_ENV')
        self.assertNotIn('openai_base_url',doc)
        disable(self.cfg);self.assertEqual(tomlkit.parse((self.home/'config.toml').read_text())['model_providers']['mine']['base_url'],'https://gateway.invalid/v1')

    def test_does_not_replace_foreign_mcp_entry(self):
        with (self.home/'config.toml').open('a') as f:f.write('\n[mcp_servers.codexpic]\ncommand="someone-else"\n')
        with self.assertRaises(GuardError):enable(self.home,self.cfg,start=False)
