import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from telethon import types
from moderator.config import Config
from moderator.engine import Engine
from moderator.llm import Classifier, Verdict, ReviewUnavailable
from moderator.store import Store
from moderator.telegram import content_of, member_present
CHAT=-1000000000100
NOW=datetime(2026,9,22,tzinfo=timezone.utc)
def cfg(**kw):
    return Config(api_id=123,api_hash='dummy',bot_token='1:dummy',chats={CHAT},llm_key='secret',
                  llm_model='test-model',llm_url=kw.pop('llm_url','http://model.test/v1'),**kw)
def update(mid=1,text='hello',uid=42,bot=False,edit=False,**kw):
    m=types.Message(id=mid,peer_id=types.PeerChannel(100),from_id=types.PeerUser(uid),date=NOW,message=text,**kw)
    cls=types.UpdateEditChannelMessage if edit else types.UpdateNewChannelMessage
    u=cls(m,mid,1); u._entities={uid:types.User(id=uid,bot=bot,first_name='User',access_hash=123)}
    return u
class FakeTG:
    own_id=1
    def __init__(self):
        self.calls=[]; self.bots={99}; self.outsiders={99}; self.admins=set(); self.bio='普通简介'; self.unban_fail=False
    async def user(self,uid): return types.User(id=uid,bot=uid in self.bots,first_name='User',username='user',access_hash=123)
    async def protected(self,chat,uid): return uid in self.admins or uid==1
    async def profile(self,uid): return {'nickname':'User','bio':self.bio}
    async def outside(self,chat,uid): return uid in self.outsiders
    async def delete(self,chat,mid): self.calls.append(('delete',mid))
    async def ban(self,chat,uid): self.calls.append(('ban',uid))
    async def unban(self,chat,uid):
        if self.unban_fail: raise RuntimeError('unban unavailable')
        self.calls.append(('unban',uid))
class FakeLLM:
    def __init__(self): self.calls=[]; self.fail=False; self.score=.99
    async def classify(self,kind,data):
        self.calls.append((kind,data))
        if self.fail: raise ReviewUnavailable('test outage')
        ad='广告' in json.dumps(data,ensure_ascii=False)
        return Verdict(ad,self.score,'测试结果','广告' if ad else '')
class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'state.db'
        self.store=Store(self.path); self.cfg=cfg(); self.tg=FakeTG(); self.llm=FakeLLM()
        self.engine=Engine(self.cfg,self.store,self.tg,self.llm)
    async def asyncTearDown(self): self.store.close(); self.tmp.cleanup()
    async def drain(self):
        for _ in range(50):
            j=self.store.claim()
            if not j: return
            await self.engine.execute(j); self.store.finish(j)
        self.fail('job loop')
    async def join(self): self.engine.join(CHAT,42,100); await self.drain()
    async def test_profile_bio(self):
        self.tg.bio='广告'; await self.join()
        self.assertEqual(self.tg.calls,[('ban',42),('unban',42)])
    async def test_fourth_not_reviewed(self):
        await self.join()
        for n in range(1,4): await self.engine.ingest(update(n))
        await self.engine.ingest(update(4,'广告')); await self.drain()
        self.assertFalse(self.tg.calls)
        self.assertEqual(len([v for v in self.llm.calls if v[0]=='message']),3)
    async def test_third_ad(self):
        await self.join()
        for n in (1,2): await self.engine.ingest(update(n))
        await self.engine.ingest(update(3,'广告')); await self.drain()
        self.assertIn(('ban',42),self.tg.calls); self.assertIn(('delete',3),self.tg.calls)
    async def test_restart_count(self):
        await self.join()
        for n in (1,2): await self.engine.ingest(update(n))
        await self.drain(); self.store.close(); self.store=Store(self.path); self.engine.store=self.store
        await self.engine.ingest(update(3,'广告')); await self.drain()
        self.assertIn(('ban',42),self.tg.calls)
    async def test_edit_first_after_fourth(self):
        await self.join()
        for n in range(1,5): await self.engine.ingest(update(n))
        await self.drain(); await self.engine.ingest(update(1,'广告',edit=True)); await self.drain()
        self.assertIn(('ban',42),self.tg.calls)
    async def test_dedup(self):
        await self.join(); await self.engine.ingest(update()); await self.drain(); count=len(self.llm.calls)
        await self.engine.ingest(update()); await self.drain(); self.assertEqual(len(self.llm.calls),count)
    async def test_edit_invalidates_old_result(self):
        await self.join(); await self.engine.ingest(update(text='广告')); old=self.store.claim()
        await self.engine.ingest(update(text='hello',edit=True))
        await self.engine.execute(old); self.store.finish(old); await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_guest_ad(self):
        await self.engine.ingest(update(uid=99,bot=True,text='广告',guestchat_via_from=types.PeerUser(42)))
        await self.drain(); self.assertEqual(self.tg.calls,[('delete',1),('ban',42),('unban',42)])
    async def test_clean_guest(self):
        await self.engine.ingest(update(uid=99,bot=True,guestchat_via_from=types.PeerUser(42)))
        await self.drain(); self.assertEqual(self.tg.calls,[('delete',1)])
    async def test_guest_channel(self):
        await self.engine.ingest(update(uid=99,bot=True,text='广告',guestchat_via_from=types.PeerChannel(999)))
        await self.drain(); self.assertEqual(self.tg.calls,[('delete',1)])
    async def test_inline_after_three(self):
        await self.join()
        for n in range(1,4): await self.engine.ingest(update(n))
        await self.drain(); await self.engine.ingest(update(4,'广告',via_bot_id=99)); await self.drain()
        self.assertIn(('ban',42),self.tg.calls)
    async def test_inline_member_bot_probation(self):
        await self.join(); self.tg.outsiders.clear()
        await self.engine.ingest(update(text='广告',via_bot_id=99)); await self.drain()
        self.assertIn(('ban',42),self.tg.calls)
    async def test_member_bot(self):
        self.tg.outsiders.clear(); await self.engine.ingest(update(uid=99,bot=True,text='广告')); await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_admin(self):
        self.tg.admins.add(42)
        await self.engine.ingest(update(uid=99,bot=True,text='广告',guestchat_via_from=types.PeerUser(42)))
        await self.drain(); self.assertEqual(self.tg.calls,[('delete',1)])
    async def test_low_confidence(self):
        self.llm.score=.4; self.tg.bio='广告'; await self.join(); self.assertFalse(self.tg.calls)
    async def test_dry_run(self):
        self.cfg.dry_run=True; self.tg.bio='广告'; await self.join(); self.assertFalse(self.tg.calls)
    async def test_retry_and_no_punishment(self):
        await self.join(); self.llm.fail=True; await self.engine.ingest(update(text='广告'))
        worker=asyncio.create_task(self.engine.worker()); await asyncio.sleep(.05); worker.cancel()
        with self.assertRaises(asyncio.CancelledError): await worker
        row=self.store.db.execute("SELECT status,attempts FROM jobs WHERE kind='message'").fetchone()
        self.assertEqual(tuple(row),('pending',1)); self.assertFalse(self.tg.calls)
    async def test_delete_survives_llm_outage(self):
        self.llm.fail=True
        await self.engine.ingest(update(uid=99,bot=True,text='广告',guestchat_via_from=types.PeerUser(42)))
        j=self.store.claim(False)
        with self.assertRaises(ReviewUnavailable): await self.engine.execute(j)
        j=self.store.claim(True); self.assertEqual(j['kind'],'delete'); await self.engine.execute(j)
        self.assertEqual(self.tg.calls,[('delete',1)])
    async def test_unban_recovery(self):
        self.tg.bio='广告'; self.engine.join(CHAT,42,100)
        j=self.store.claim(); await self.engine.execute(j); self.store.finish(j)
        j=self.store.claim(); self.tg.unban_fail=True
        with self.assertRaises(RuntimeError): await self.engine.execute(j)
        self.store.fail(j,'unban',8,0); self.tg.unban_fail=False; await self.drain()
        self.assertEqual(self.tg.calls,[('ban',42),('unban',42)])
    async def test_rejoin_invalidates_old_review(self):
        await self.join(); await self.engine.ingest(update(text='广告'))
        self.store.leave(CHAT,42,101); self.engine.join(CHAT,42,102); await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_join_raw_fallbacks(self):
        u=types.UpdateChannelParticipant(channel_id=100,date=NOW,actor_id=42,user_id=42,qts=1,
          prev_participant=types.ChannelParticipantLeft(types.PeerUser(42)),new_participant=types.ChannelParticipant(42,NOW))
        await self.engine.ingest(u)
        m=types.MessageService(id=50,peer_id=types.PeerChannel(100),from_id=types.PeerUser(42),date=NOW,
                               action=types.MessageActionChatJoinedByRequest())
        await self.engine.ingest(types.UpdateNewChannelMessage(m,1,1))
        self.assertEqual(self.store.member(CHAT,42)['epoch'],1)
    async def test_hidden_link(self):
        m=update(text='这里').message; m.entities=[types.MessageEntityTextUrl(0,2,'https://ads.test')]
        self.assertIn('https://ads.test',content_of(m))
    async def test_membership_error_not_outside(self):
        async def broken(*a): raise RuntimeError('network')
        self.tg.outside=broken; await self.engine.ingest(update(uid=99,bot=True))
        with self.assertRaises(RuntimeError): await self.engine.execute(self.store.claim())
        self.assertFalse(self.tg.calls)
    async def test_existing_and_other_chat(self):
        await self.engine.ingest(update(text='广告'))
        u=update(text='广告'); u.message.peer_id=types.PeerChannel(999)
        await self.engine.ingest(u); await self.drain(); self.assertFalse(self.llm.calls)
    async def test_engine_with_real_classifier_adapter(self):
        def handler(req):
            body=json.loads(req.content)
            self.assertEqual(json.loads(body['messages'][1]['content'])['kind'],'external_bot')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':
                '{"is_ad":true,"confidence":0.99,"reason":"推广","evidence":"广告"}'}}]})
        classifier=Classifier(self.cfg,httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        self.engine.llm=classifier
        try:
            await self.engine.ingest(update(uid=99,bot=True,text='广告',guestchat_via_from=types.PeerUser(42)))
            await self.drain()
            self.assertEqual(self.tg.calls,[('delete',1),('ban',42),('unban',42)])
        finally: await classifier.close()
    async def test_failed_tasks_can_be_requeued(self):
        self.store.put('sample','profile',{'chat':CHAT,'user':42,'epoch':1})
        job=self.store.claim()
        self.assertEqual(self.store.fail(job,'error',1,0),'failed')
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.retry_failed(),1)
        self.assertEqual(self.store.claim()['attempts'],0)
    async def test_known_bot_mention_alone_does_not_kick(self):
        await self.join()
        await self.engine.ingest(update(text='@somebot 请计算 1+1'))
        await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_queued_kick_invalidated_by_clean_edit(self):
        await self.join(); await self.engine.ingest(update(text='广告'))
        job=self.store.claim(False); await self.engine.execute(job); self.store.finish(job)
        await self.engine.ingest(update(text='hello',edit=True))
        await self.drain()
        self.assertFalse(self.tg.calls)

class LLMTests(unittest.IsolatedAsyncioTestCase):
    async def classify(self,value=None,status=200,config=None,finish='stop'):
        self.request=None
        def handler(req):
            self.request=req
            return httpx.Response(status,json={'choices':[{'finish_reason':finish,'message':{'content':value}}]})
        c=Classifier(config or cfg(),httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try: return await c.classify('message',{'text':'广告联系我'})
        finally: await c.close()
    async def test_request_format(self):
        result=await self.classify(json.dumps(dict(is_ad=True,confidence=.98,reason='推广',evidence='广告')))
        self.assertTrue(result.is_ad); self.assertEqual(str(self.request.url),'http://model.test/v1/chat/completions')
        self.assertEqual(self.request.headers['Authorization'],'Bearer secret')
        b=json.loads(self.request.content); self.assertEqual(b['response_format'],{'type':'json_object'})
        self.assertNotIn('广告联系我',b['messages'][0]['content'])
    async def test_json_mode_optional(self):
        await self.classify('{"is_ad":false,"confidence":0.9,"reason":"正常","evidence":""}',config=cfg(llm_json_mode=False))
        self.assertNotIn('response_format',json.loads(self.request.content))
    async def test_invalid_boolean(self):
        with self.assertRaises(ReviewUnavailable): await self.classify('{"is_ad":"false","confidence":0.9,"reason":"x","evidence":""}')
    async def test_invalid_confidence(self):
        for v in ('NaN','1.5','true'):
            with self.assertRaises(ReviewUnavailable): await self.classify('{"is_ad":false,"confidence":'+v+',"reason":"x","evidence":""}')
    async def test_fake_evidence(self):
        with self.assertRaises(ReviewUnavailable): await self.classify('{"is_ad":true,"confidence":0.99,"reason":"x","evidence":"不存在"}')
    async def test_http_failure(self):
        with self.assertRaises(ReviewUnavailable): await self.classify(status=429)
    async def test_truncated(self):
        with self.assertRaises(ReviewUnavailable): await self.classify('{}',finish='length')
    async def test_real_local_http(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                body=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                    dict(is_ad=True,confidence=.99,reason='推广',evidence='广告'))}}]}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        c=Classifier(cfg(llm_url=f'http://127.0.0.1:{server.server_port}/v1'))
        try:
            result=await c.classify('message',{'text':'广告'})
            self.assertTrue(result.is_ad); self.assertEqual(seen[0][0],'/v1/chat/completions')
        finally:
            await c.close(); await asyncio.to_thread(server.shutdown); server.server_close(); thread.join()
class ConfigTests(unittest.TestCase):
    def test_dotenv_dollar(self):
        text=Path('.env.example').read_text().replace('replace_with_your_api_hash','abc').replace(
          'replace_with_your_bot_token','1:dummy').replace('replace_with_your_llm_api_key','sk-$literal').replace(
          'replace_with_your_model_id','my-model')
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{},clear=True):
            p=Path(d)/'.env'; p.write_text(text); c=Config.load(p)
            self.assertEqual(c.llm_key,'sk-$literal'); self.assertEqual(c.llm_model,'my-model')
    def test_banned_status(self):
        p=types.ChannelParticipantBanned(peer=types.PeerUser(42),kicked_by=1,date=NOW,
             banned_rights=types.ChatBannedRights(until_date=None,view_messages=True),left=True)
        self.assertFalse(member_present(p))
if __name__=='__main__': unittest.main()
