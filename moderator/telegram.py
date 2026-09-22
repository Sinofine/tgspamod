from datetime import datetime, timezone
from telethon import types, functions, errors, utils


def member_present(p):
    if p is None or isinstance(p, types.ChannelParticipantLeft): return False
    if isinstance(p, types.ChannelParticipantBanned):
        return not p.left and not p.banned_rights.view_messages
    return True


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


class Gateway:
    def __init__(self, client, cfg, own_id):
        self.client,self.cfg,self.own_id = client,cfg,own_id

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
        return {'nickname':' '.join(v for v in (user.first_name,user.last_name) if v),
                'username':user.username or '', 'bio':result.full_user.about or ''}

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
