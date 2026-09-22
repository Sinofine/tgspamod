"""Entry point. All Telegram traffic uses Telethon / MTProto."""
import argparse
import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import sys
from telethon import TelegramClient, events
from moderator.config import Config
from moderator.engine import Engine
from moderator.llm import Classifier
from moderator.store import Store
from moderator.telegram import Gateway

LOG = logging.getLogger('moderator')

async def run(cfg):
    client = TelegramClient(str(cfg.data_dir / 'telegram'), cfg.api_id, cfg.api_hash,
        proxy=cfg.tg_proxy, sequential_updates=True, catch_up=True,
        flood_sleep_threshold=0, request_retries=2)
    store = Store(cfg.data_dir / 'moderation.sqlite3')
    classifier = Classifier(cfg)
    gateway = Gateway(client,cfg,int(cfg.bot_token.split(':',1)[0]))
    engine = Engine(cfg,store,gateway,classifier)
    async def receive(update):
        # Persist access hashes along with incoming data, before queueing work.
        entities = getattr(update, '_entities', {})
        if entities:
            client.session.process_entities(list(entities.values()))
            client.session.save()
        await engine.ingest(update)
    client.add_event_handler(receive,events.Raw())
    tasks = []
    try:
        await client.start(bot_token=cfg.bot_token)
        me = await client.get_me()
        if not me.bot or me.id != gateway.own_id:
            raise RuntimeError('当前 session 与 TG_BOT_TOKEN 不匹配，请使用独立 DATA_DIR')
        await gateway.validate()
        LOG.info('机器人已启动 id=%s groups=%s dry_run=%s',me.id,len(cfg.chats),cfg.dry_run)
        # Independent action worker cannot get stuck waiting for an LLM semaphore.
        tasks = [asyncio.create_task(engine.worker()) for _ in range(cfg.llm_concurrency)]
        tasks.append(asyncio.create_task(engine.worker(actions=True)))
        disconnected = asyncio.create_task(client.run_until_disconnected())
        tasks.append(disconnected)
        done,_ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in done: await task
    finally:
        for task in tasks: task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError): await task
        await client.disconnect()
        await classifier.close()
        store.close()


async def check_llm(cfg):
    classifier = Classifier(cfg)
    try:
        result = await classifier.classify('message', {'text': '大家好，请问这个 Python 报错应该如何排查？'})
        print(f'模型接口调用及 JSON 校验通过：is_ad={result.is_ad}, confidence={result.confidence}')
        print('这是连通性检查，不是广告识别准确率评测。')
    finally:
        await classifier.close()


def main():
    parser = argparse.ArgumentParser(description='Telethon + 大语言模型群审核机器人')
    parser.add_argument('--env',default='.env',help='配置文件路径')
    parser.add_argument('--check-config',action='store_true',help='只校验配置，不联网')
    parser.add_argument('--check-llm',action='store_true',help='用一条固定样例检查模型接口，不连接 Telegram')
    parser.add_argument('--retry-failed',action='store_true',help='将失败任务重新排队后启动')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('telethon').setLevel(logging.WARNING)
    os.umask(0o077)
    try:
        cfg = Config.load(args.env)
        if args.check_config:
            print('配置校验通过（未验证凭据有效性/模型接口连接）。')
            return
        if args.check_llm:
            asyncio.run(check_llm(cfg))
            return
        cfg.data_dir.mkdir(parents=True,exist_ok=True)
        # Prevent two processes from acting on the same session/database.
        import fcntl
        with (cfg.data_dir / 'process.lock').open('w') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('此 DATA_DIR 已有机器人实例运行') from None
            if args.retry_failed:
                store = Store(cfg.data_dir / 'moderation.sqlite3')
                print(f'重新排队 {store.retry_failed()} 个失败任务')
                store.close()
            asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except ValueError as error:
        # Config errors use field names; never print environment values.
        LOG.error('配置错误：%s',error)
        sys.exit(1)
    except Exception as error:
        LOG.error('启动/运行失败：%s；请检查权限、网络与凭据。',type(error).__name__)
        sys.exit(1)

if __name__ == '__main__': main()
