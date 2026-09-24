import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import types, functions, errors
from moderator.channel_log import ChannelLog, render_report
from moderator.engine import Engine
from moderator.store import Store
from moderator.telegram import Gateway
from test_moderator import cfg, FakeTG, FakeLLM, CHAT, update

class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:'); self.tg=FakeTG();self.llm=FakeLLM()
        self.cfg=cfg(audit_channel='@audit_test')
        self.tg.publish_report=AsyncMock(return_value=123)
        self.e=Engine(self.cfg,self.store,self.tg,self.llm)
        self.e.join(CHAT,42,100);await self.drain()
    async def asyncTearDown(self):self.store.close()
    async def drain(self):
        for _ in range(40):
            job=self.store.claim()
            if not job:return
            await self.e.execute(job);self.store.finish(job)
        self.fail('queue stuck')
    def reports(self):return self.store.db.execute('SELECT * FROM channel_reports ORDER BY id').fetchall()
    async def ad(self):
        await self.e.ingest(update(text='广告'));await self.drain()
    async def test_default_ignores_normal_reviews(self):
        self.assertFalse(self.reports())
        await self.e.ingest(update());await self.drain();self.assertFalse(self.reports())
    async def test_all_includes_normal_review(self):
        self.cfg.audit_channel_mode='all'
        await self.e.ingest(update());await self.drain()
        self.assertEqual(len(self.reports()),1)
    async def test_report_tracks_actual_actions_and_edit(self):
        await self.ad();row=self.reports()[0];body=json.loads(row['body'])
        self.assertEqual(body['actions']['删除消息'],'成功')
        self.assertEqual(body['actions']['封禁用户'],'成功')
        self.assertIn('成功',body['actions']['解除封禁'])
        await self.e.channel_log.deliver_once()
        self.assertEqual(self.reports()[0]['message_id'],123)
        self.e.channel_log.action(row['source'],row['source_revision'],'备注','更新')
        await self.e.channel_log.deliver_once()
        self.assertEqual(self.tg.publish_report.call_args.args[3],123)
    async def test_low_confidence_not_claimed_as_punished(self):
        self.llm.score=.76;await self.ad()
        body=json.loads(self.reports()[0]['body'])
        self.assertIn('低于阈值',body['actions']['封禁用户']);self.assertFalse(self.tg.calls)
    async def test_dry_run_still_publishes_but_no_punishment(self):
        self.cfg.dry_run=True;await self.ad()
        self.assertFalse(self.tg.calls)
        body=json.loads(self.reports()[0]['body']);self.assertEqual(body['actions']['封禁用户'],'仅模拟')
        await self.e.channel_log.deliver_once();self.tg.publish_report.assert_awaited_once()
    async def test_delivery_failure_does_not_repeat_moderation(self):
        await self.ad();count=len(self.llm.calls)
        self.tg.publish_report.side_effect=RuntimeError('no rights')
        await self.e.channel_log.deliver_once()
        self.assertEqual(len(self.llm.calls),count)
        self.assertEqual(self.reports()[0]['attempts'],1)
        self.assertGreater(self.reports()[0]['due'],0)
    async def test_revision_during_send_is_not_lost(self):
        await self.ad();row=self.reports()[0]
        async def publish(*args):
            self.e.channel_log.action(row['source'],row['source_revision'],'备注','new')
            return 123
        self.tg.publish_report.side_effect=publish
        await self.e.channel_log.deliver_once()
        row=self.reports()[0];self.assertLess(row['sent_version'],row['version'])
        self.assertEqual(row['message_id'],123)
    async def test_source_edit_invalidates_report(self):
        await self.ad()
        self.store.put(f'message:{CHAT}:1','message',{'edited':True},replace=True)
        self.assertEqual(self.reports()[0]['stale'],1)
    async def test_persisted_random_id_reused_on_retry(self):
        await self.ad();rid=self.reports()[0]['random_id']
        self.tg.publish_report.side_effect=RuntimeError()
        await self.e.channel_log.deliver_once()
        self.store.db.execute('UPDATE channel_reports SET due=0');self.store.db.commit()
        self.tg.publish_report.side_effect=None
        other=ChannelLog(self.cfg,self.store,self.tg);await other.deliver_once()
        self.assertEqual(self.tg.publish_report.call_args.args[2],rid)
    async def test_failed_delivery_can_be_requeued(self):
        self.cfg.max_attempts=1;await self.ad()
        self.tg.publish_report.side_effect=RuntimeError()
        await self.e.channel_log.deliver_once();self.assertEqual(self.reports()[0]['failed'],1)
        self.store.retry_failed();self.assertEqual(self.reports()[0]['failed'],0)
    async def test_no_html_injection_and_bounded_content(self):
        await self.ad();data=json.loads(self.reports()[0]['body'])
        data['input']={'text':'<a href="https://evil">evil</a>'+'x'*20000}
        html=render_report(data)
        self.assertNotIn('<a href=',html);self.assertIn('&lt;a',html)
        self.assertIn('已截断',html);self.assertLess(len(html),12000)

class RichGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_rich_send_and_edit(self):
        entity=types.Channel(id=777,title='Audit',photo=types.ChatPhotoEmpty(),date=None,broadcast=True,access_hash=123)
        peer=types.InputPeerChannel(777,123)
        calls=[]
        async def invoke(request):
            calls.append(request)
            if isinstance(request,functions.messages.SendMessageRequest):
                return types.Updates(updates=[types.UpdateMessageID(456,request.random_id)],users=[],chats=[],date=None,seq=1)
            raise errors.MessageNotModifiedError(request)
        class Client:
            get_entity=AsyncMock(return_value=entity)
            get_input_entity=AsyncMock(return_value=peer)
            __call__=staticmethod(invoke)
        gateway=Gateway(Client(),cfg(),1)
        self.assertEqual(await gateway.publish_report('@audit','<table></table>',123),456)
        self.assertTrue(calls[0].rich_message.noautolink)
        self.assertIsInstance(calls[0].rich_message,types.InputRichMessageHTML)
        self.assertTrue(bytes(calls[0]))
        self.assertEqual(await gateway.publish_report('@audit','new',123,456),456)
    async def test_non_channel_rejected(self):
        client=SimpleNamespace(get_entity=AsyncMock(return_value=types.User(123)))
        with self.assertRaises(ValueError):await Gateway(client,cfg(),1).publish_report('@audit','x',1)

class ActionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_then_skipped_is_not_left_waiting(self):
        store=Store(':memory:')
        try:
            config=cfg(audit_channel='@audit_test');log=ChannelLog(config,store,None)
            job={'key':'x','revision':1,'payload':{'chat':CHAT,'user':42}}
            log.record(job,'profile',42,actions={'封禁用户':'等待执行'})
            log.action('x',1,'封禁用户','失败，等待重试：TimeoutError')
            log.action('x',1,'封禁用户','跳过：已失效',only_pending=True)
            body=json.loads(store.db.execute('select body from channel_reports').fetchone()[0])
            self.assertEqual(body['actions']['封禁用户'],'跳过：已失效')
            log.action('x',1,'封禁用户','成功')
            log.action('x',1,'封禁用户','跳过',only_pending=True)
            body=json.loads(store.db.execute('select body from channel_reports').fetchone()[0])
            self.assertEqual(body['actions']['封禁用户'],'成功')
        finally:store.close()
