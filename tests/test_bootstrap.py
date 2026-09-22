import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch
import unittest
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import SQLiteSession
from moderator.bootstrap import ChatBootstrap
from moderator.config import Config
from moderator.telegram import Gateway

CHAT = -1005100328224
NOW = datetime(2026,9,22,tzinfo=timezone.utc)

class OfflineClient(TelegramClient):
    def __init__(self, path):
        super().__init__(SQLiteSession(str(path)),123,'dummy')
        self.permission_calls=[]
        self.admin=True
        self.resolved_id=5100328224
        self.lookup_calls=[]
    async def __call__(self, request, *args, **kwargs):
        if isinstance(request,functions.channels.GetChannelsRequest):
            # Reproduce the bot's zero-access-hash lookup failing on a fresh session.
            raise errors.ChannelInvalidError(request)
        raise AssertionError(type(request).__name__)
    async def get_entity(self, entity):
        if isinstance(entity,str) and entity.startswith('@'):
            self.lookup_calls.append(entity)
            return channel(self.resolved_id)
        return await super().get_entity(entity)
    async def get_permissions(self, entity, user=None):
        assert isinstance(entity,types.InputPeerChannel)
        assert entity.access_hash==987654321
        assert user=='me'
        self.permission_calls.append(entity.channel_id)
        return SimpleNamespace(is_admin=self.admin,delete_messages=self.admin,ban_users=self.admin)
    async def get_dialogs(self,*args,**kwargs):
        raise AssertionError('getDialogs is not a bot API')

def channel(uid=5100328224):
    return types.Channel(id=uid,title='Test',photo=types.ChatPhotoEmpty(),date=NOW,
                         megagroup=True,access_hash=987654321)

class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'telegram'
        self.client=OfflineClient(self.path)
        self.cfg=Config(api_id=123,api_hash='dummy',bot_token='1:dummy',chats={CHAT},
                        llm_url='http://localhost/v1',llm_key='dummy',llm_model='dummy')
        self.boot=ChatBootstrap(self.client,self.cfg,Gateway(self.client,self.cfg,1))
    async def asyncTearDown(self):
        await self.client.disconnect()
        self.tmp.cleanup()
    def incoming(self, uid=5100328224):
        return SimpleNamespace(_entities={-1000000000000-uid:channel(uid)})
    async def test_original_missing_entity_reproduced(self):
        with self.assertRaisesRegex(ValueError,'Could not find the input entity'):
            await self.client.get_input_entity(CHAT)
    async def test_missing_entity_stays_pending_without_permission_call(self):
        await self.boot.refresh()
        self.assertFalse(self.boot.is_ready(CHAT))
        self.assertFalse(self.client.permission_calls)
    async def test_incoming_entity_enables_group(self):
        await self.boot.refresh()
        self.boot.remember(self.incoming())
        self.assertTrue(self.boot.changed.is_set())
        await self.boot.refresh()
        self.assertTrue(self.boot.is_ready(CHAT))
        self.assertEqual(self.client.permission_calls,[5100328224])
    async def test_watch_wakes_on_incoming_update(self):
        await self.boot.refresh()
        watcher=asyncio.create_task(self.boot.watch())
        self.boot.remember(self.incoming())
        for _ in range(20):
            if self.boot.is_ready(CHAT): break
            await asyncio.sleep(.01)
        self.assertTrue(self.boot.is_ready(CHAT))
        self.assertFalse(watcher.done())
        watcher.cancel()
        with self.assertRaises(asyncio.CancelledError): await watcher
    async def test_cache_survives_restart(self):
        self.boot.remember(self.incoming())
        await self.client.disconnect()
        self.client=OfflineClient(self.path)
        self.boot=ChatBootstrap(self.client,self.cfg,Gateway(self.client,self.cfg,1))
        await self.boot.refresh()
        self.assertTrue(self.boot.is_ready(CHAT))
        self.assertFalse(self.client.lookup_calls)
    async def test_public_username_seed(self):
        self.cfg.chat_references={CHAT:'@test_group'}
        await self.boot.refresh()
        self.assertTrue(self.boot.is_ready(CHAT))
        self.assertEqual(self.client.lookup_calls,['@test_group'])
    async def test_public_username_wrong_id_does_not_enable(self):
        self.cfg.chat_references={CHAT:'@wrong_group'}
        self.client.resolved_id=999
        await self.boot.refresh()
        self.assertFalse(self.boot.is_ready(CHAT))
        self.assertFalse(self.client.permission_calls)
    async def test_permission_grant_recovers(self):
        self.boot.remember(self.incoming())
        self.client.admin=False
        await self.boot.refresh()
        self.assertFalse(self.boot.is_ready(CHAT))
        self.client.admin=True
        await self.boot.refresh()
        self.assertTrue(self.boot.is_ready(CHAT))
    async def test_other_group_update_does_not_enable_target(self):
        self.boot.remember(self.incoming(999))
        await self.boot.refresh()
        self.assertFalse(self.boot.is_ready(CHAT))
    async def test_ready_group_not_rechecked(self):
        self.boot.remember(self.incoming())
        await self.boot.refresh(); await self.boot.refresh()
        self.assertEqual(len(self.client.permission_calls),1)

class EntryPointTests(unittest.TestCase):
    def test_runtime_entity_error_not_reported_as_config(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            cfg=Config(api_id=123,api_hash='dummy',bot_token='1:dummy',chats={CHAT},
                llm_url='http://localhost/v1',llm_key='dummy',llm_model='dummy',data_dir=Path(d))
            async def fail(_): raise ValueError('Could not find the input entity')
            with patch('sys.argv',['bot.py']), patch.object(bot.Config,'load',return_value=cfg), \
                 patch.object(bot,'run',side_effect=fail), patch.object(bot.logging,'basicConfig'), patch.object(bot.os,'umask'), \
                 self.assertLogs('moderator',level='ERROR') as logs, self.assertRaises(SystemExit):
                bot.main()
            self.assertIn('运行时实体/参数解析失败',logs.output[0])
            self.assertNotIn('配置文件读取/校验失败',logs.output[0])
    def test_reference_config(self):
        values={'TG_API_ID':'123','TG_API_HASH':'dummy','TG_BOT_TOKEN':'1:dummy',
                'TG_CHAT_IDS':str(CHAT),'LLM_BASE_URL':'http://localhost/v1',
                'LLM_API_KEY':'dummy','LLM_MODEL':'dummy',
                'TG_CHAT_REFERENCES':'{"-1005100328224":"@test_group"}'}
        with patch.dict('os.environ',values,clear=True), patch('moderator.config.load_dotenv'):
            cfg=Config.load()
            self.assertEqual(cfg.chat_references,{CHAT:'@test_group'})
        values['TG_CHAT_REFERENCES']='{"-100999":"@test_group"}'
        with patch.dict('os.environ',values,clear=True), patch('moderator.config.load_dotenv'):
            with self.assertRaisesRegex(ValueError,'TG_CHAT_REFERENCES'):
                Config.load()

if __name__=='__main__': unittest.main()
