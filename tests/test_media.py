import asyncio
import sqlite3
import tempfile
from pathlib import Path
import unittest
from telethon import types
from moderator.engine import Engine
from moderator.store import Store
from test_moderator import cfg, update, FakeTG, FakeLLM, CHAT

class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'state.db'
        self.store=Store(self.path); self.config=cfg(); self.tg=FakeTG(); self.llm=FakeLLM()
        self.engine=Engine(self.config,self.store,self.tg,self.llm)
        self.engine.join(CHAT,42,100); await self.drain()
        self.llm.calls.clear()
    async def asyncTearDown(self): self.store.close(); self.tmp.cleanup()
    async def drain(self):
        for _ in range(60):
            job=self.store.claim()
            if not job: return
            await self.engine.execute(job); self.store.finish(job)
        self.fail('queue loop')
    def slots(self):
        return self.store.db.execute('SELECT count(*) FROM messages WHERE checked>=0').fetchone()[0]
    async def send_media(self,mid=1,text='',media=None,**kw):
        await self.engine.ingest(update(mid,text,media=media or types.MessageMediaDocument(),**kw))
    async def test_video_deleted_no_slot_no_llm_no_kick(self):
        await self.send_media(); await self.drain()
        self.assertEqual(self.tg.calls,[('delete',1)])
        self.assertEqual(self.slots(),0); self.assertFalse(self.llm.calls)
    async def test_photo_with_caption_still_deleted(self):
        await self.send_media(text='正常说明',media=types.MessageMediaPhoto()); await self.drain()
        self.assertEqual(self.tg.calls,[('delete',1)]); self.assertFalse(self.llm.calls)
        self.assertEqual(self.slots(),0)
    async def test_three_media_cannot_bypass(self):
        for mid in range(1,4): await self.send_media(mid)
        await self.drain(); self.assertEqual(self.slots(),0)
        await self.engine.ingest(update(4,'广告')); await self.drain()
        self.assertIn(('ban',42),self.tg.calls)
    async def test_media_allowed_after_three_clean_reviews(self):
        for mid in range(1,4): await self.engine.ingest(update(mid,'你好'))
        await self.drain(); self.llm.calls.clear()
        await self.send_media(4); await self.drain()
        self.assertFalse(self.tg.calls); self.assertFalse(self.llm.calls)
    async def test_three_pending_reviews_do_not_unlock_media(self):
        for mid in range(1,4): await self.engine.ingest(update(mid,'你好'))
        await self.send_media(4)
        first=self.store.claim(True)
        self.assertEqual(first['kind'],'media')
        await self.engine.execute(first); self.store.finish(first)
        self.assertEqual(self.tg.calls,[('delete',4)])
        self.assertFalse(self.llm.calls)
    async def test_edit_counted_text_to_media_revokes_pass(self):
        for mid in range(1,4): await self.engine.ingest(update(mid,'你好'))
        await self.drain()
        await self.send_media(1,edit=True); await self.drain()
        self.assertIn(('delete',1),self.tg.calls)
        self.assertEqual(self.slots(),2)
        await self.engine.ingest(update(4,'你好')); await self.drain()
        await self.send_media(5); await self.drain()
        self.assertNotIn(('delete',5),self.tg.calls)
    async def test_clean_edit_cancels_queued_media_delete(self):
        await self.send_media(1)
        await self.engine.ingest(update(1,'你好',edit=True))
        await self.drain(); self.assertFalse(self.tg.calls)
    async def test_admin_exempt(self):
        self.tg.admins.add(42); await self.send_media(); await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_dry_run(self):
        self.config.dry_run=True; await self.send_media(); await self.drain()
        self.assertFalse(self.tg.calls)
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM audit WHERE action='would_delete_media'").fetchone())
    async def test_existing_member_unaffected(self):
        await self.send_media(uid=88); await self.drain()
        self.assertFalse(self.tg.calls)
    async def test_optional_unseen_member_restricted(self):
        self.config.check_unseen=True; await self.send_media(uid=88); await self.drain()
        self.assertEqual(self.tg.calls,[('delete',1)])
    async def test_disable_restores_old_counting(self):
        self.config.restrict_newcomer_media=False
        await self.send_media(); await self.drain()
        self.assertFalse(self.tg.calls); self.assertEqual(self.slots(),1)
    async def test_restart_keeps_media_restriction(self):
        await self.engine.ingest(update(1,'你好')); await self.drain()
        self.store.close(); self.store=Store(self.path); self.engine.store=self.store
        await self.send_media(2); await self.drain()
        self.assertEqual(self.tg.calls,[('delete',2)])
    async def test_inline_external_does_not_use_slot(self):
        await self.engine.ingest(update(1,'正常结果',via_bot_id=99)); await self.drain()
        self.assertEqual(self.slots(),0)
        for mid in range(2,5): await self.engine.ingest(update(mid,'你好'))
        await self.drain(); await self.send_media(5); await self.drain()
        self.assertNotIn(('delete',5),self.tg.calls)
    async def test_text_link_preview_allowed(self):
        media=types.MessageMediaWebPage(types.WebPageEmpty(id=1))
        await self.engine.ingest(update(1,'https://example.com',media=media)); await self.drain()
        self.assertFalse(self.tg.calls); self.assertEqual(self.slots(),1)
    async def test_old_running_review_cannot_pass_after_media_edit(self):
        await self.engine.ingest(update(1,'你好')); job=self.store.claim(False)
        await self.send_media(1,edit=True)
        await self.engine.execute(job); self.store.finish(job); await self.drain()
        self.assertEqual(self.slots(),0)
        self.assertEqual(self.tg.calls,[('delete',1)])

class MigrationTests(unittest.TestCase):
    def test_legacy_database_keeps_existing_slots(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'old.db'; db=sqlite3.connect(path)
            db.execute('CREATE TABLE messages(chat INTEGER,user INTEGER,epoch INTEGER,message INTEGER,PRIMARY KEY(chat,user,epoch,message))')
            db.execute('INSERT INTO messages VALUES(1,2,1,3)');db.commit();db.close()
            store=Store(path)
            self.assertEqual(store.db.execute('SELECT checked FROM messages').fetchone()[0],1)
            store.close()

if __name__=='__main__': unittest.main()
