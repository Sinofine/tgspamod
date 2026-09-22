"""Resolve configured groups before moderation; bot accounts cannot get dialogs."""
import asyncio
import logging
import time
from telethon import errors, utils

LOG = logging.getLogger('moderator')

class ChatBootstrap:
    def __init__(self, client, cfg, gateway):
        self.client, self.cfg, self.gateway = client, cfg, gateway
        self.ready = set()
        self.changed = asyncio.Event()
        self.last_warning = {}
        self.next_attempt = {}

    def is_ready(self, chat):
        return chat in self.ready

    def remember(self, update):
        entities = getattr(update, '_entities', {})
        if entities:
            # Updates carry the access hashes that numeric PeerChannel IDs lack.
            self.client.session.process_entities(list(entities.values()))
            self.client.session.save()
            if any(chat in entities for chat in self.cfg.chats - self.ready):
                self.changed.set()

    def warn(self, chat, text):
        if self.last_warning.get(chat) != text:
            LOG.warning('群 %s：%s', chat, text)
            self.last_warning[chat] = text

    async def refresh(self):
        for chat in sorted(self.cfg.chats - self.ready):
            if time.monotonic() < self.next_attempt.get(chat, 0):
                continue
            try:
                try:
                    peer = await self.client.get_input_entity(chat)
                except ValueError:
                    reference = self.cfg.chat_references.get(chat)
                    if not reference:
                        self.warn(chat, '尚未取得群访问资料；保持监听。请确认 bot 已加入群并设为管理员，'
                                  '然后在该群发送 /start@机器人用户名；公开群也可配置 TG_CHAT_REFERENCES。')
                        continue
                    entity = await self.client.get_entity(reference)
                    if utils.get_peer_id(entity) != chat:
                        self.warn(chat, 'TG_CHAT_REFERENCES 的用户名与配置的群 ID 不一致，未启用审核。')
                        continue
                    self.client.session.process_entities([entity])
                    self.client.session.save()
                    peer = utils.get_input_peer(entity)
                await self.gateway.validate_chat(chat, peer)
            except errors.FloodWaitError as error:
                self.next_attempt[chat] = time.monotonic() + error.seconds + 1
                self.warn(chat, f'Telegram 限流，{error.seconds + 1} 秒后重试群初始化。')
            except (errors.ChatAdminRequiredError, errors.ChannelPrivateError,
                    errors.UserNotParticipantError):
                self.warn(chat, '无法访问或无管理权限；请确认 bot 已在群内并有删除消息、封禁用户权限。')
            except GroupPermissionError as error:
                self.warn(chat, str(error))
            except Exception as error:
                # Do not expose arbitrary exception text containing request data.
                self.warn(chat, f'群初始化暂未完成（{type(error).__name__}），将重试。')
                self.next_attempt[chat] = time.monotonic() + 10
            else:
                self.ready.add(chat)
                self.last_warning.pop(chat, None)
                LOG.info('群 %s：群资料与管理员权限确认完成，审核已启用。', chat)

    async def watch(self):
        # Remain alive even after all groups are ready: main supervises this task.
        while True:
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            self.changed.clear()
            await self.refresh()

class GroupPermissionError(Exception):
    """Controlled message safe to show in initialization diagnostics."""
