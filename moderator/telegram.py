from datetime import datetime, timezone
from telethon import types, functions, errors, utils


def member_present(p):
    if p is None or isinstance(p, types.ChannelParticipantLeft): return False
    if isinstance(p, types.ChannelParticipantBanned):
        return not p.left and not p.banned_rights.view_messages
    return True


def unreviewable_media(msg):
    # A normal URL preview is treated as a text link; attached files are not.
    return (getattr(msg, 'rich_message', None) is not None or
            (msg.media is not None and not isinstance(msg.media,
                (types.MessageMediaEmpty, types.MessageMediaWebPage))))


def content_of(msg):
    parts = [msg.message or '']
    for entity in msg.entities or []:
        if isinstance(entity, types.MessageEntityTextUrl): parts.append(entity.url)
    for row in getattr(msg.reply_markup, 'rows', []) or []:
        for button in row.buttons:
            parts.extend([getattr(button,'text',''),getattr(button,'url','')])
    doc = getattr(msg.media, 'document', None)
    for attr in getattr(doc, 'attributes', []) or []:
        if isinstance(attr, types.DocumentAttributeFilename): parts.append(attr.file_name)
    poll = getattr(msg.media, 'poll', None)
    def plain(obj): return obj if isinstance(obj,str) else getattr(obj,'text','')
    if poll:
        parts.append(plain(poll.question))
        parts.extend(plain(a.text) for a in poll.answers)
    rich = getattr(msg, 'rich_message', None)
    def walk(value):
        if isinstance(value,dict):
            for key,v in value.items():
                if key in ('text','url') and isinstance(v,str): parts.append(v)
                elif isinstance(v,(list,dict)): walk(v)
        elif isinstance(value,list):
            for v in value: walk(v)
    if rich: walk(rich.to_dict())
    return '\n'.join(p for p in parts if p)


def reply_of(msg):
    header = getattr(msg, 'reply_to', None)
    if not isinstance(header, types.MessageReplyHeader): return None
    peer = getattr(header, 'reply_to_peer_id', None)
    external = peer is not None and utils.get_peer_id(peer) != utils.get_peer_id(msg.peer_id)
    quote = getattr(header, 'quote_text', None) or ''
    urls = [e.url for e in (getattr(header, 'quote_entities', None) or [])
            if isinstance(e, types.MessageEntityTextUrl)]
    origin = getattr(header, 'reply_from', None)
    names = {key:getattr(origin, key, None) for key in ('from_name','post_author')
             if getattr(origin, key, None)}
    if not external and not quote and not urls and not names: return None
    return {'external':external, 'peer':utils.get_peer_id(peer) if peer else None,
            'message':getattr(header, 'reply_to_msg_id', None),
            'quote_text':quote, 'quote_urls':urls, 'origin_labels':names}


class Gateway:
    def __init__(self, client, cfg, own_id):
        self.client,self.cfg,self.own_id = client,cfg,own_id

    async def publish_report(self, destination, html, random_id, message_id=None):
        reference=int(destination) if destination.startswith('-') else destination
        entity=await self.client.get_entity(reference)
        if not isinstance(entity,types.Channel) or not entity.broadcast:
            raise ValueError('Audit destination must be a broadcast channel')
        if utils.get_peer_id(entity) in self.cfg.chats:
            raise ValueError('Audit channel cannot be moderated')
        peer=await self.client.get_input_entity(entity)
        rich=types.InputRichMessageHTML(html=html,noautolink=True)
        if message_id is not None:
            try:
                await self.client(functions.messages.EditMessageRequest(peer=peer,id=message_id,rich_message=rich))
            except errors.MessageNotModifiedError:
                pass
            return message_id
        result=await self.client(functions.messages.SendMessageRequest(
            peer=peer,message='',rich_message=rich,random_id=random_id,silent=True,no_webpage=True))
        if isinstance(result,types.UpdateShortSentMessage): return result.id
        for update in getattr(result,'updates',[]):
            if isinstance(update,types.UpdateMessageID) and update.random_id==random_id:return update.id
        for update in getattr(result,'updates',[]):
            msg=getattr(update,'message',None)
            if isinstance(msg,types.Message) and msg.out and utils.get_peer_id(msg.peer_id)==utils.get_peer_id(entity):
                return msg.id
        raise RuntimeError('Sent audit message ID unavailable')

    async def user(self, uid):
        return await self.client.get_entity(types.PeerUser(uid))

    async def participant(self, chat, uid):
        try:
            if utils.resolve_id(chat)[1] is types.PeerChannel:
                response = await self.client(functions.channels.GetParticipantRequest(chat,types.PeerUser(uid)))
                return response.participant
            info = await self.client(functions.messages.GetFullChatRequest(-chat))
            participants = getattr(info.full_chat.participants,'participants',None)
            if participants is None: raise RuntimeError('Basic group participant list unavailable')
            return next((p for p in participants if p.user_id == uid),None)
        except errors.UserNotParticipantError:
            return None

    async def protected(self, chat, uid):
        if uid == self.own_id or uid in self.cfg.exempt: return True
        p = await self.participant(chat,uid)
        return isinstance(p,(types.ChannelParticipantAdmin,types.ChannelParticipantCreator,
                             types.ChatParticipantAdmin,types.ChatParticipantCreator))

    async def profile(self, uid):
        user = await self.user(uid)
        result = await self.client(functions.users.GetFullUserRequest(user))
        # Prefer the fresh user returned with full info over a cached entity.
        user = next((u for u in getattr(result, 'users', []) if u.id == uid), user)
        profile = {'nickname':' '.join(v for v in (user.first_name,user.last_name) if v),
                   'username':user.username or '', 'bio':result.full_user.about or ''}
        status = getattr(user, 'emoji_status', None)
        if isinstance(status, types.EmojiStatus):
            until = status.until
            if until is not None:
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if until <= datetime.now(timezone.utc):
                    return profile
            documents = await self.client(functions.messages.GetCustomEmojiDocumentsRequest(
                document_id=[status.document_id]))
            document = next((d for d in documents if d.id == status.document_id), None)
            attr = next((a for a in getattr(document, 'attributes', [])
                         if isinstance(a, types.DocumentAttributeCustomEmoji)), None)
            if attr is None or isinstance(attr.stickerset, types.InputStickerSetEmpty):
                raise RuntimeError('Emoji status stickerset unavailable')
            # Fetch current names: pack owners can rename a set without changing its ID.
            response = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=attr.stickerset, hash=0))
            pack = getattr(response, 'set', None)
            if pack is None or not isinstance(pack.title, str) or not isinstance(pack.short_name, str):
                raise RuntimeError('Emoji status stickerset metadata unavailable')
            if isinstance(attr.stickerset, types.InputStickerSetID) and pack.id != attr.stickerset.id:
                raise RuntimeError('Emoji status stickerset mismatch')
            profile['emoji_status_pack'] = {'title':pack.title, 'short_name':pack.short_name}
        return profile

    async def reply_text(self, peer, mid):
        if peer is None or not isinstance(mid, int) or mid <= 0:
            raise RuntimeError('Reply target reference unavailable')
        message = await self.client.get_messages(peer, ids=mid)
        if not isinstance(message, types.Message) or message.id != mid:
            raise RuntimeError('Reply target message unavailable')
        if utils.get_peer_id(message.peer_id) != peer:
            raise RuntimeError('Reply target peer mismatch')
        # Only this one target; do not recursively follow replies or fetch history.
        text = content_of(message)
        if not text:
            raise RuntimeError('Reply target has no reviewable text')
        return text

    async def outside(self, chat, uid):
        return not member_present(await self.participant(chat,uid))

    async def delete(self, chat, mid):
        try:
            await self.client.delete_messages(chat,[mid],revoke=True)
        except errors.MessageIdInvalidError:
            # Already deleted, or no longer a valid message; idempotent action.
            pass

    async def ban(self, chat, uid):
        if utils.resolve_id(chat)[1] is types.PeerChannel:
            await self.client(functions.channels.EditBannedRequest(chat,types.PeerUser(uid),
                types.ChatBannedRights(until_date=None,view_messages=True)))
        else:
            if self.cfg.kick_mode == 'ban':
                raise RuntimeError('Permanent ban requires a supergroup')
            await self.client(functions.messages.DeleteChatUserRequest(-chat,utils.get_input_user(await self.client.get_input_entity(types.PeerUser(uid)))))

    async def unban(self, chat, uid):
        if utils.resolve_id(chat)[1] is types.PeerChannel:
            await self.client(functions.channels.EditBannedRequest(chat,types.PeerUser(uid),
                types.ChatBannedRights(until_date=None)))

    async def validate_chat(self, chat, peer):
        from .bootstrap import GroupPermissionError
        permissions = await self.client.get_permissions(peer, 'me')
        if not permissions.is_admin or not permissions.delete_messages or not permissions.ban_users:
            raise GroupPermissionError('需要管理员、删除消息和封禁用户权限；授权后会自动重试。')
        if self.cfg.kick_mode == 'ban' and utils.resolve_id(chat)[1] is not types.PeerChannel:
            raise GroupPermissionError('KICK_MODE=ban 只支持超级群；普通群请使用 kick 并重启。')
