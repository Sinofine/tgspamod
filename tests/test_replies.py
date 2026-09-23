import json
import unittest
import httpx
from types import SimpleNamespace
from telethon import types
from moderator.telegram import Gateway, reply_of
from moderator.engine import Engine
from moderator.store import Store
from moderator.llm import Classifier
from test_moderator import cfg, FakeTG, update, CHAT

OTHER=-1000000000200
def header(quote=None,peer=200,**kw):
    return types.MessageReplyHeader(reply_to_msg_id=777,reply_to_peer_id=types.PeerChannel(peer),quote_text=quote,**kw)

class ReplyExtractionTests(unittest.TestCase):
    def test_quote_and_hidden_url_separate_from_body(self):
        msg=update(text='看看',reply_to=header('链接',quote_entities=[types.MessageEntityTextUrl(0,2,'https://spam.test')])).message
        r=reply_of(msg)
        self.assertTrue(r['external']);self.assertEqual(r['peer'],OTHER)
        self.assertEqual(r['quote_text'],'链接');self.assertEqual(r['quote_urls'],['https://spam.test'])
    def test_plain_same_chat_reply_is_unchanged(self):
        self.assertIsNone(reply_of(update(reply_to=header(peer=100)).message))
    def test_same_chat_quote_is_identified_as_local(self):
        self.assertFalse(reply_of(update(reply_to=header('引用',peer=100)).message)['external'])

class ReplyGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_single_message_request(self):
        calls=[]
        m=update(mid=777,text='联系我购买').message;m.peer_id=types.PeerChannel(200)
        async def get_messages(peer,ids):calls.append((peer,ids));return m
        g=Gateway(SimpleNamespace(get_messages=get_messages),cfg(),1)
        self.assertEqual(await g.reply_text(OTHER,777),'联系我购买')
        self.assertEqual(calls,[(OTHER,777)])
    async def test_missing_or_wrong_peer_not_accepted(self):
        for m in (None, types.MessageEmpty(777, types.PeerChannel(200)), update(mid=777).message):
            async def get_messages(peer,ids):return m
            with self.assertRaises(RuntimeError):
                await Gateway(SimpleNamespace(get_messages=get_messages),cfg(),1).reply_text(OTHER,777)

class ReplyEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.tg=FakeTG();self.requests=[];self.fetches=[]
        async def fetch(peer,mid):self.fetches.append((peer,mid));return '联系我购买'
        self.tg.reply_text=fetch
        def handler(req):
            data=json.loads(json.loads(req.content)['messages'][1]['content'])['data']
            self.requests.append(data)
            # Deliberately scripted classifier: verifies wiring, not model accuracy.
            ad='联系我购买' in json.dumps(data,ensure_ascii=False) and '举报' not in data.get('text','')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'is_ad':ad,'confidence':.99,'reason':'模拟传播广告' if ad else '正常或举报',
                'evidence':'联系我购买' if ad else ''},ensure_ascii=False)}}]})
        self.llm=Classifier(cfg(),httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        self.e=Engine(cfg(),self.store,self.tg,self.llm)
        self.e.join(CHAT,42,100);await self.drain();self.requests.clear()
    async def asyncTearDown(self):await self.llm.close();self.store.close()
    async def drain(self):
        for _ in range(50):
            j=self.store.claim()
            if not j:return
            await self.e.execute(j);self.store.finish(j)
        self.fail('loop')
    async def test_quoted_spam_reaches_model_and_targets_sender_only(self):
        await self.e.ingest(update(text='🙂',reply_to=header('联系我购买')));await self.drain()
        self.assertEqual(self.requests[0]['text'],'🙂')
        self.assertEqual(self.requests[0]['reply_context']['quote_text'],'联系我购买')
        self.assertEqual(self.tg.calls,[('delete',1),('ban',42),('unban',42)])
        self.assertFalse(self.fetches)
    async def test_report_not_auto_punished_due_to_reply(self):
        await self.e.ingest(update(text='举报这个骗子',reply_to=header('联系我购买')));await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_fetch_when_quote_absent(self):
        await self.e.ingest(update(text='🙂',reply_to=header()));await self.drain()
        self.assertEqual(self.fetches,[(OTHER,777)])
        self.assertIn(('ban',42),self.tg.calls)
    async def test_fetch_failure_retries_without_punishment_or_pass(self):
        async def fail(peer,mid):raise RuntimeError('unavailable')
        self.tg.reply_text=fail
        await self.e.ingest(update(text='🙂',reply_to=header()));j=self.store.claim(False)
        with self.assertRaises(RuntimeError):await self.e.execute(j)
        self.store.fail(j,'unavailable',8,10)
        self.assertFalse(self.tg.calls);self.assertFalse(self.requests)
        self.assertEqual(self.store.db.execute('select checked from messages').fetchone()[0],0)
    async def test_quote_only_edit_invalidates_old_result(self):
        await self.e.ingest(update(text='🙂',reply_to=header('联系我购买')))
        old=self.store.claim(False)
        await self.e.ingest(update(text='🙂',reply_to=header('普通文字'),edit=True))
        await self.e.execute(old);self.store.finish(old);await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_fourth_reply_not_new_moderation_scope(self):
        for mid in range(1,4):await self.e.ingest(update(mid=mid))
        await self.drain();self.requests.clear()
        await self.e.ingest(update(mid=4,text='🙂',reply_to=header('联系我购买')));await self.drain()
        self.assertFalse(self.tg.calls);self.assertFalse(self.requests)
    async def test_admin_protected(self):
        self.tg.admins.add(42)
        await self.e.ingest(update(text='🙂',reply_to=header('联系我购买')));await self.drain()
        self.assertFalse(self.tg.calls)
