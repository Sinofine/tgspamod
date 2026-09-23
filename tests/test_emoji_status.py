from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import json
import unittest
import httpx
from telethon import types, functions
from moderator.telegram import Gateway
from moderator.engine import Engine
from moderator.store import Store
from moderator.llm import Classifier
from test_moderator import cfg, FakeTG, CHAT

class ProfileClient:
    def __init__(self, status=None, title='普通表情', short_name='ordinary_emojis'):
        self.current=types.User(id=42,first_name='User',emoji_status=status)
        self.calls=[]; self.title=title; self.short_name=short_name
        self.failure=None; self.missing=False
    async def get_entity(self, peer):
        return types.User(id=42,first_name='Cached')
    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.users.GetFullUserRequest):
            return SimpleNamespace(users=[self.current],full_user=SimpleNamespace(about='普通简介'))
        if self.failure: raise self.failure
        if isinstance(request, functions.messages.GetCustomEmojiDocumentsRequest):
            if self.missing:return []
            return [types.Document(id=123,access_hash=1,file_reference=b'',date=None,
                mime_type='application/x-tgsticker',size=1,dc_id=1,attributes=[
                    types.DocumentAttributeCustomEmoji(alt='🙂',stickerset=types.InputStickerSetID(456,789))])]
        if isinstance(request, functions.messages.GetStickerSetRequest):
            return SimpleNamespace(set=types.StickerSet(id=456,access_hash=789,title=self.title,
                short_name=self.short_name,count=1,hash=1))
        raise AssertionError(type(request))

class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_status_and_both_names(self):
        c=ProfileClient(types.EmojiStatus(123), '广告联系我', 'contact_us')
        p=await Gateway(c,cfg(),1).profile(42)
        self.assertEqual(p['nickname'],'User')
        self.assertEqual(p['emoji_status_pack'],{'title':'广告联系我','short_name':'contact_us'})
        self.assertEqual(c.calls[1].document_id,[123])
        self.assertEqual(c.calls[2].stickerset.id,456)
        self.assertEqual(c.calls[2].hash,0)
    async def test_absent_empty_expired_no_extra_requests(self):
        for status in (None, types.EmojiStatusEmpty(),
                       types.EmojiStatus(123,datetime.now(timezone.utc)-timedelta(seconds=10))):
            c=ProfileClient(status);p=await Gateway(c,cfg(),1).profile(42)
            self.assertNotIn('emoji_status_pack',p);self.assertEqual(len(c.calls),1)
    async def test_missing_document_not_clean(self):
        c=ProfileClient(types.EmojiStatus(123));c.missing=True
        with self.assertRaises(RuntimeError):await Gateway(c,cfg(),1).profile(42)
    async def test_rpc_failure_propagates(self):
        c=ProfileClient(types.EmojiStatus(123));c.failure=RuntimeError('offline')
        with self.assertRaises(RuntimeError):await Gateway(c,cfg(),1).profile(42)
    async def test_renamed_pack_fetched_again(self):
        c=ProfileClient(types.EmojiStatus(123));g=Gateway(c,cfg(),1)
        await g.profile(42);c.title='广告联系我'
        self.assertEqual((await g.profile(42))['emoji_status_pack']['title'],'广告联系我')

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, title='普通表情', short_name='ordinary', failure=False, low=False):
        c=ProfileClient(types.EmojiStatus(123),title,short_name)
        if failure:c.failure=RuntimeError('offline')
        config=cfg();gateway=Gateway(c,config,1);tg=FakeTG();tg.profile=gateway.profile
        seen=[]
        def response(req):
            body=json.loads(req.content);data=json.loads(body['messages'][1]['content'])['data'];seen.append(data)
            pack=data.get('emoji_status_pack',{})
            ad='广告联系我' in pack.values()
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'is_ad':ad,'confidence':.4 if low else .99,'reason':'状态表情包名称含招揽' if ad else '正常',
                'evidence':'广告联系我' if ad else ''},ensure_ascii=False)}}]})
        llm=Classifier(config,httpx.AsyncClient(transport=httpx.MockTransport(response)))
        s=Store(':memory:');e=Engine(config,s,tg,llm);e.join(CHAT,42,100)
        try:
            with self.assertLogs('moderator',level='INFO') as logs:
                j=s.claim(False)
                try:await e.execute(j)
                except RuntimeError:s.fail(j,'offline',8,10)
                else:s.finish(j)
                for _ in range(10):
                    j=s.claim(True)
                    if not j:break
                    await e.execute(j);s.finish(j)
            return tg.calls,seen,logs.output,s.db.execute("select status from jobs where kind='profile'").fetchone()[0]
        finally:await llm.close();s.close()
    async def test_ad_pack_reaches_classifier_and_kicks(self):
        calls,seen,logs,status=await self.scenario(title='广告联系我')
        self.assertEqual(calls,[('ban',42),('unban',42)])
        self.assertEqual(seen[0]['emoji_status_pack']['title'],'广告联系我')
        self.assertTrue(any('review_input' in x and '广告联系我' in x for x in logs))
        self.assertTrue(any('review_result' in x and 'evidence' in x for x in logs))
    async def test_short_name_also_reviewed(self):
        calls,_,_,_=await self.scenario(short_name='广告联系我')
        self.assertIn(('ban',42),calls)
    async def test_normal_pack_no_punishment(self):
        calls,_,_,status=await self.scenario();self.assertFalse(calls);self.assertEqual(status,'done')
    async def test_low_confidence_no_punishment(self):
        calls,_,_,_=await self.scenario(title='广告联系我',low=True);self.assertFalse(calls)
    async def test_failure_stays_pending_without_punishment(self):
        calls,seen,_,status=await self.scenario(failure=True)
        self.assertFalse(calls);self.assertEqual(status,'pending')
        self.assertNotIn('emoji_status_pack',seen[0])
