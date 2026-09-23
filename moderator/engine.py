import asyncio
import logging
import json
import time
from telethon import types, utils, errors
from .telegram import content_of, member_present, unreviewable_media, reply_of

LOG = logging.getLogger('moderator')


def timestamp(date):
    return int(date.timestamp()) if date else int(time.time())

class Engine:
    def __init__(self, cfg, store, gateway, classifier, is_ready=None):
        self.cfg,self.store,self.tg,self.llm = cfg,store,gateway,classifier
        self.is_ready = is_ready or (lambda chat: True)

    def note(self, chat, uid, action, detail):
        self.store.audit(chat,uid,action,detail)
        # LLM-generated reasons may quote personal text: keep console minimal.
        LOG.info('chat=%s user=%s action=%s',chat,uid,action)

    def join(self, chat, uid, stamp):
        if uid == self.tg.own_id: return
        epoch = self.store.join(chat,uid,stamp)
        if epoch is not None:
            self.store.put(f'profile:{chat}:{uid}:{epoch}', 'profile',
                           {'chat':chat,'user':uid,'epoch':epoch})

    async def ingest(self, update):
        """No network awaits: reserve first-three slots before any LLM work."""
        if isinstance(update, types.UpdateChannelParticipant):
            chat = utils.get_peer_id(types.PeerChannel(update.channel_id))
            if chat not in self.cfg.chats: return
            old,new = member_present(update.prev_participant),member_present(update.new_participant)
            if not old and new: self.join(chat,update.user_id,timestamp(update.date))
            elif old and not new: self.store.leave(chat,update.user_id,timestamp(update.date))
            return
        if isinstance(update,(types.UpdateChatParticipantAdd,types.UpdateChatParticipantDelete)):
            chat = -update.chat_id
            if chat not in self.cfg.chats: return
            if isinstance(update,types.UpdateChatParticipantAdd):
                self.join(chat,update.user_id,timestamp(update.date))
            else: self.store.leave(chat,update.user_id,int(time.time()))
            return
        edited = isinstance(update,(types.UpdateEditMessage,types.UpdateEditChannelMessage))
        if not isinstance(update,(types.UpdateNewMessage,types.UpdateNewChannelMessage,
                                  types.UpdateEditMessage,types.UpdateEditChannelMessage)): return
        msg = update.message
        if not isinstance(msg,(types.Message,types.MessageService)): return
        chat = utils.get_peer_id(msg.peer_id)
        if chat not in self.cfg.chats: return
        if isinstance(msg,types.MessageService):
            action = msg.action
            if isinstance(action,types.MessageActionChatAddUser):
                for uid in action.users: self.join(chat,uid,timestamp(msg.date))
            elif isinstance(action,(types.MessageActionChatJoinedByLink,
                                    types.MessageActionChatJoinedByRequest,
                                    types.MessageActionChatJoinedViaCommunity)):
                if isinstance(msg.from_id,types.PeerUser):
                    self.join(chat,msg.from_id.user_id,timestamp(msg.date))
            elif isinstance(action,types.MessageActionChatDeleteUser):
                self.store.leave(chat,action.user_id,timestamp(msg.date))
            return
        if not isinstance(msg.from_id,types.PeerUser): return
        uid = msg.from_id.user_id
        if uid == self.tg.own_id: return
        caller = getattr(msg,'guestchat_via_from',None)
        caller_id = caller.user_id if isinstance(caller,types.PeerUser) else None
        # User entity comes from the update container; gateway resolves if absent.
        entities = getattr(update,'_entities',{})
        sender = entities.get(uid)
        is_bot = getattr(sender,'bot',None)
        external_candidate = bool(caller or msg.via_bot_id or is_bot)
        epoch = None
        restricted_media = False
        if self.cfg.restrict_newcomer_media and unreviewable_media(msg) and not caller and is_bot is not True:
            epoch = self.store.media_epoch(chat,uid,msg.id,edited,self.cfg.check_unseen)
            restricted_media = epoch is not None
        if not restricted_media and not caller and is_bot is not True:
            epoch = self.store.first_three(chat,uid,msg.id,edited,self.cfg.check_unseen)
        # Unknown sender type must still be resolved, even after probation.
        if not external_candidate and epoch is None and is_bot is not None:
            if not self.store.has_job(f'message:{chat}:{msg.id}'): return
        data = {'chat':chat,'user':uid,'message':msg.id,'text':content_of(msg),
                'epoch':epoch,'restricted_media':restricted_media,'guest':caller is not None,'caller':caller_id,
                'via_bot':msg.via_bot_id,'is_bot':is_bot,
                'edit_stamp':timestamp(msg.edit_date) if msg.edit_date else 0,
                'reply':reply_of(msg)}
        if caller_id is not None or (msg.via_bot_id and is_bot is not True):
            member = self.store.member(chat,caller_id if caller_id is not None else uid)
            data['caller_epoch'] = member['epoch'] if member else None
        key = f'message:{chat}:{msg.id}'
        changed = self.store.put(key,'message',data,replace=True)
        if changed and epoch is not None and not restricted_media:
            self.store.mark_checked(chat,uid,epoch,msg.id,0)
        if restricted_media:
            self.store.put(f'media:{chat}:{msg.id}','media',
                {'chat':chat,'user':uid,'epoch':epoch,'message':msg.id,
                 'source':{'key':key,'revision':self.store.revision(key)}},replace=True)

    def valid_epoch(self, p, user=None, epoch=None):
        uid = p['user'] if user is None else user
        expected = p.get('epoch') if epoch is None else epoch
        if expected is None: return True
        member = self.store.member(p['chat'],uid)
        return bool(member and member['active'] and member['epoch'] == expected)

    def delete_job(self, chat, mid, source=None):
        payload = {'chat':chat,'message':mid,'user':0}
        if source:
            payload['source'] = {'key':source['key'],'revision':source['revision']}
        self.store.put(f'delete:{chat}:{mid}','delete',payload,replace=True)

    async def review(self, job, kind, data, target, epoch):
        p = job['payload']
        if await self.tg.protected(p['chat'],target): return
        if kind == 'external_bot' and (await self.tg.user(target)).bot: return
        if not self.store.current(job) or not self.valid_epoch(p,target,epoch): return
        # JSON quoting keeps user-controlled newlines from forging log entries.
        LOG.info('review_input chat=%s user=%s kind=%s job=%s revision=%s data=%s',
                 p['chat'],target,kind,job['key'],job['revision'],
                 json.dumps(data,ensure_ascii=False))
        result = await self.llm.classify(kind,data)
        if not self.store.current(job): return
        if not self.valid_epoch(p,target,epoch): return
        if kind == 'message':
            self.store.mark_checked(p['chat'],target,epoch,p['message'],1 if not result.is_ad else -1)
        verdict = json.dumps({'kind':kind,'is_ad':result.is_ad,
                              'confidence':result.confidence,'reason':result.reason,
                              'evidence':result.evidence},ensure_ascii=False)
        LOG.info('review_result chat=%s user=%s job=%s revision=%s result=%s',
                 p['chat'],target,job['key'],job['revision'],verdict)
        self.note(p['chat'],target,'review',verdict)
        if result.is_ad and result.confidence >= self.cfg.threshold:
            if kind == 'message': self.delete_job(p['chat'],p['message'],job)
            payload = {'chat':p['chat'],'user':target,'epoch':epoch,'phase':'ban',
                       'source':job['key'],'source_revision':job['revision']}
            self.store.put(f"kick:{job['key']}:{job['revision']}",'kick',payload)

    async def message_data(self, p):
        data = {'text':p['text']}
        reply = p.get('reply')
        if reply:
            data['reply_context'] = dict(reply)
            if reply['external'] and not reply.get('quote_text') and not reply.get('quote_urls'):
                data['reply_context']['target_text'] = await self.tg.reply_text(
                    reply.get('peer'),reply.get('message'))
        return data

    async def execute(self, job):
        p,kind = job['payload'],job['kind']
        if kind == 'profile':
            if not self.valid_epoch(p): return
            user = await self.tg.user(p['user'])
            if user.bot or await self.tg.protected(p['chat'],p['user']): return
            # Name is checked even if fetching bio later fails.
            name = {'nickname':' '.join(v for v in (user.first_name,user.last_name) if v),
                    'username':user.username or ''}
            try:
                profile = await self.tg.profile(p['user'])
            except Exception:
                await self.review(job,'profile',name,p['user'],p['epoch'])
                raise
            await self.review(job,'profile',profile,p['user'],p['epoch'])
        elif kind == 'message':
            is_bot = p['is_bot']
            if is_bot is None: is_bot = (await self.tg.user(p['user'])).bot
            source_bot = p['user'] if is_bot else p['via_bot']
            outside = bool(p['guest']) or (source_bot and await self.tg.outside(p['chat'],source_bot))
            if outside:
                if p['epoch'] is not None and not is_bot:
                    self.store.mark_checked(p['chat'],p['user'],p['epoch'],p['message'],-1)
                self.delete_job(p['chat'],p['message'])
                target = p['caller'] if p['guest'] else (p['user'] if p['via_bot'] and not is_bot else None)
                epoch = p.get('caller_epoch')
                if target and (p['text'] or p.get('reply')):
                    await self.review(job,'external_bot',await self.message_data(p),target,epoch)
            elif not is_bot and not p.get('restricted_media') and p['epoch'] is not None and self.valid_epoch(p) and (p['text'] or p.get('reply')):
                await self.review(job,'message',await self.message_data(p),p['user'],p['epoch'])
        elif kind == 'media':
            if not self.store.current(p['source']) or not self.valid_epoch(p): return
            if await self.tg.protected(p['chat'],p['user']): return
            if (await self.tg.user(p['user'])).bot: return
            if not self.cfg.dry_run: await self.tg.delete(p['chat'],p['message'])
            self.note(p['chat'],p['user'],'would_delete_media' if self.cfg.dry_run else 'deleted_media',
                      'newcomer media restriction')
        elif kind == 'delete':
            if p.get('source') and not self.store.current(p['source']): return
            if not self.cfg.dry_run: await self.tg.delete(p['chat'],p['message'])
            self.note(p['chat'],0,'would_delete' if self.cfg.dry_run else 'deleted',str(p['message']))
        elif kind == 'kick':
            # An unban is a recovery step for this job's successful ban.
            if p['phase'] == 'ban':
                source = {'key':p['source'],'revision':p['source_revision']}
                if not self.store.current(source) or not self.valid_epoch(p): return
                if await self.tg.protected(p['chat'],p['user']): return
                if await self.tg.outside(p['chat'],p['user']): return
                if self.cfg.dry_run:
                    self.note(p['chat'],p['user'],'would_kick',self.cfg.kick_mode)
                    return
                await self.tg.ban(p['chat'],p['user'])
                p['phase'] = 'unban'
                self.store.checkpoint(job)
                self.note(p['chat'],p['user'],'banned',self.cfg.kick_mode)
            if self.cfg.kick_mode == 'kick':
                await self.tg.unban(p['chat'],p['user'])
                self.note(p['chat'],p['user'],'unbanned','rejoin allowed')

    async def worker(self, actions=False):
        while True:
            job = self.store.claim(actions)
            if job is None:
                await asyncio.sleep(0.25)
                continue
            if not self.is_ready(job['payload']['chat']):
                self.store.defer(job,1)
                continue
            try:
                await self.execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Persist retries; never turn errors into a clean verdict or punishment.
                delay = min(self.cfg.retry_seconds * 2 ** min(job['attempts'],6),3600)
                if isinstance(error,errors.FloodWaitError): delay = max(delay,error.seconds+1)
                detail = type(error).__name__
                from .llm import ReviewUnavailable
                if isinstance(error,ReviewUnavailable): detail = str(error)
                status = self.store.fail(job,detail,self.cfg.max_attempts,delay)
                LOG.error('job=%s status=%s error=%s',job['key'],status,detail)
            else:
                self.store.finish(job)
