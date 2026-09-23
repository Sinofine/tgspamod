"""Resolve configured groups before moderation; bot accounts cannot get dialogs."""
import asyncio
import logging
import time
from telethon import errors, utils, types

LOG = logging.getLogger('moderator')

class ChatBootstrap:
    def __init__(self, client, cfg, gateway):
        self.client, self.cfg, self.gateway = client, cfg, gateway
        self.ready = set()
        self.changed = asyncio.Event()
        self.last_warning = {}
        self.next_attempt = {}
        self.update_count = 0
        self.chat_update_count = {}
        self.last_status = time.monotonic()

    def report_waiting(self):
        pending = self.cfg.chats - self.ready
        if not pending or time.monotonic() - self.last_status < 60:
            return
        self.last_status = time.monotonic()
        for chat in sorted(pending):
            LOG.warning('群 %s：仍在等待初始化；连接=%s，收到更新总数=%s，'
                        '该群更新数=%s。%s', chat, self.client.is_connected(),
                        self.update_count, self.chat_update_count.get(chat, 0),
                        '尚未收到该群更新，请检查群 ID、机器人身份及其他运行实例。'
                        if not self.chat_update_count.get(chat, 0) else
                        '已收到该群更新，但资料或权限尚未确认；请查看初始化警告。')

    def is_ready(self, chat):
        return chat in self.ready

    def remember(self, update):
        self.update_count += 1
        msg = getattr(update, 'message', None)
        peer = getattr(msg, 'peer_id', None)
        chat = None
        if peer is not None:
            chat = utils.get_peer_id(peer)
        elif getattr(update, 'channel_id', None) is not None:
            chat = utils.get_peer_id(types.PeerChannel(update.channel_id))
        elif getattr(update, 'chat_id', None) is not None:
            chat = -update.chat_id
        if chat in self.cfg.chats:
            count = self.chat_update_count.get(chat, 0) + 1
            self.chat_update_count[chat] = count
            if count == 1:
                LOG.info('群 %s：已收到首个群更新（%s）。', chat, type(update).__name__)
            if chat not in self.ready:
                self.changed.set()
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
                                  '收到正常群更新后会自动继续，无需发送命令；公开群也可配置 TG_CHAT_REFERENCES。')
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
            self.report_waiting()

class GroupPermissionError(Exception):
    """Controlled message safe to show in initialization diagnostics."""
