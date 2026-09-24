import unittest
from telethon import types
from moderator.engine import Engine
from moderator.store import Store
from test_moderator import cfg, FakeTG, FakeLLM, CHAT, update

class ProfileUpdatesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.tg=FakeTG();self.llm=FakeLLM()
        self.e=Engine(cfg(),self.store,self.tg,self.llm)
        self.e.join(CHAT,42,100);await self.drain();self.llm.calls.clear()
    async def asyncTearDown(self):self.store.close()
    async def drain(self):
        for _ in range(40):
            j=self.store.claim()
            if not j:return
            await self.e.execute(j);self.store.finish(j)
        self.fail('queue did not settle')
    def passed(self):
        for mid in range(1,4):
            self.store.first_three(CHAT,42,mid,False)
            self.store.mark_checked(CHAT,42,1,mid,1)
    async def test_bio_event_reaudits_and_does_not_consume_slots(self):
        self.tg.bio='广告'
        await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertEqual(len(self.llm.calls),1)
        self.assertEqual(self.tg.calls,[('ban',42),('unban',42)])
        self.assertEqual(self.store.db.execute('select count(*) from messages').fetchone()[0],0)
    async def test_unchanged_profile_does_not_call_llm(self):
        for _ in range(2):
            await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertFalse(self.llm.calls)
    async def test_all_three_event_types_queue(self):
        for event in (types.UpdateUser(42),types.UpdateUserName(42,'new','',[]),
                      types.UpdateUserEmojiStatus(42,types.EmojiStatusEmpty())):
            await self.e.ingest(event)
            job=self.store.claim(False);self.assertEqual(job['kind'],'profile')
            await self.e.execute(job);self.store.finish(job)
    async def test_completed_members_not_rechecked(self):
        self.passed();self.tg.bio='广告'
        await self.e.ingest(types.UpdateUser(42))
        await self.e.ingest(update(mid=4));await self.drain()
        self.assertFalse(self.llm.calls);self.assertFalse(self.tg.calls)
    async def test_reserved_but_pending_slots_still_allow_profile(self):
        for mid in range(1,4):self.store.first_three(CHAT,42,mid,False)
        self.tg.bio='changed'
        await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertEqual(len(self.llm.calls),1)
    async def test_completion_while_llm_running_discards_result(self):
        self.tg.bio='广告'
        original=self.llm.classify
        async def classify(kind,data):
            self.passed();return await original(kind,data)
        self.llm.classify=classify
        await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_new_event_while_llm_running_invalidates_old_result(self):
        self.tg.bio='广告';original=self.llm.classify
        async def classify(kind,data):
            if data.get('bio')=='广告':
                self.tg.bio='normal again'
                await self.e.ingest(types.UpdateUser(42))
            return await original(kind,data)
        self.llm.classify=classify
        await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertFalse(self.tg.calls)
        self.assertEqual(len(self.llm.calls),2)
    async def test_silent_change_before_kick_rechecked(self):
        self.tg.bio='广告'
        await self.e.ingest(types.UpdateUser(42))
        job=self.store.claim(False);await self.e.execute(job);self.store.finish(job)
        self.tg.bio='normal again'
        await self.drain();self.assertFalse(self.tg.calls)
    async def test_visible_change_in_message_triggers_profile(self):
        self.tg.bio='new bio'
        event=update();event._entities[42].first_name='New Name'
        await self.e.ingest(event);await self.drain()
        self.assertTrue(any(kind=='profile' for kind,_ in self.llm.calls))
    async def test_failed_review_not_cached(self):
        self.tg.bio='new bio';self.llm.fail=True
        await self.e.ingest(types.UpdateUser(42));job=self.store.claim(False)
        with self.assertRaises(Exception):await self.e.execute(job)
        self.assertNotIn('new bio',self.store.profile_state(CHAT,42,1)['reviewed'])
        self.llm.fail=False
        await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertIn('new bio',self.store.profile_state(CHAT,42,1)['reviewed'])
    async def test_departed_unknown_and_unconfigured_members_ignored(self):
        self.store.leave(CHAT,42,200)
        self.store.join(-999,42,100)
        await self.e.ingest(types.UpdateUser(42));await self.e.ingest(types.UpdateUser(999))
        self.assertIsNone(self.store.claim())
    async def test_rejoin_does_not_reuse_old_profile_cache(self):
        self.store.leave(CHAT,42,200);self.e.join(CHAT,42,300)
        await self.drain();self.assertEqual(len(self.llm.calls),1)
    async def test_admin_not_punished_on_refresh(self):
        self.tg.admins.add(42);self.tg.bio='广告'
        await self.e.ingest(types.UpdateUser(42));await self.drain()
        self.assertFalse(self.llm.calls);self.assertFalse(self.tg.calls)
    async def test_completed_before_pending_kick_skips_punishment(self):
        self.tg.bio='广告'
        await self.e.ingest(types.UpdateUser(42));job=self.store.claim(False)
        await self.e.execute(job);self.store.finish(job)
        self.passed();await self.drain();self.assertFalse(self.tg.calls)
    async def test_event_during_final_profile_fetch_invalidates_punishment(self):
        self.tg.bio='广告'
        await self.e.ingest(types.UpdateUser(42));job=self.store.claim(False)
        await self.e.execute(job);self.store.finish(job)
        original=self.tg.profile
        async def profile(uid):
            result=await original(uid)
            self.tg.bio='safe';self.tg.profile=original
            await self.e.ingest(types.UpdateUser(uid))
            return result
        self.tg.profile=profile
        await self.drain();self.assertFalse(self.tg.calls)
