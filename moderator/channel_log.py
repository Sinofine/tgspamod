"""Persistent, independent delivery of moderation reports (not process logs)."""
import asyncio
from datetime import datetime, timezone
from html import escape
import json
import logging
import secrets
import time
from telethon import errors

LOG = logging.getLogger('moderator')


def render_report(data, stale=False):
    def text(value, limit=1500):
        value = str(value)
        if len(value) > limit: value = value[:limit] + '…［已截断，完整内容见本地日志］'
        return escape(value)
    rows = [('来源群 ID',data['chat']),('用户 ID',data['user']),
            ('审核类型',data['kind']),('源消息 ID',data.get('message') or '—'),
            ('模型',data.get('model','—')),('时间 UTC',data['time']),
            ('任务版本',data['revision'])]
    verdict = data.get('verdict')
    if verdict:
        rows += [('广告判定','是' if verdict['is_ad'] else '否'),
                 ('模型自评置信度',verdict['confidence']),('处置阈值',data['threshold'])]
    rows += [(key,value) for key,value in data.get('actions',{}).items()]
    title = '审核记录 · 旧版本已失效' if stale else '审核记录'
    html = '<h2>'+title+'</h2><table><tr><th>项目</th><th>内容</th></tr>'
    html += ''.join('<tr><td>'+text(k)+'</td><td>'+text(v,350)+'</td></tr>' for k,v in rows)
    html += '</table>'
    if stale: html += '<p>后续处置以当前版本为准；已经执行的操作不会自动撤销。</p>'
    if verdict:
        html += '<h3>理由</h3><p>'+text(verdict['reason'],700)+'</p>'
        html += '<h3>证据</h3><blockquote>'+text(verdict['evidence'],700)+'</blockquote>'
    # Only allowlisted review fields; never serialize config, secrets or raw HTTP replies.
    for key in ('text','nickname','username','bio','emoji_status_pack','reply_context'):
        if key in data.get('input',{}):
            value=data['input'][key]
            if not isinstance(value,str): value=json.dumps(value,ensure_ascii=False)
            html += '<h3>'+text(key)+'</h3><blockquote>'+text(value)+'</blockquote>'
    return html


class ChannelLog:
    def __init__(self, cfg, store, gateway):
        self.cfg,self.store,self.tg=cfg,store,gateway

    def record(self, job, kind, uid, data=None, verdict=None, actions=None):
        if not self.cfg.audit_channel: return
        if verdict and not verdict['is_ad'] and self.cfg.audit_channel_mode != 'all': return
        p=job['payload']
        body={'chat':p['chat'],'user':uid,'message':p.get('message'),'kind':kind,
              'model':self.cfg.llm_model,'threshold':self.cfg.threshold,
              'revision':job['revision'],'time':datetime.now(timezone.utc).isoformat(),
              'input':data or {},'verdict':verdict,'actions':actions or {}}
        self.store.db.execute('''INSERT OR IGNORE INTO channel_reports
            (source,source_revision,destination,body,random_id)
            VALUES(?,?,?,?,?)''',(job['key'],job['revision'],self.cfg.audit_channel,
                                  json.dumps(body,ensure_ascii=False),secrets.randbits(63)))
        self.store.db.commit()

    def action(self, source, revision, label, status, only_pending=False):
        if not self.cfg.audit_channel: return
        row=self.store.db.execute('SELECT id,body FROM channel_reports WHERE source=? AND source_revision=? AND destination=?',
                                  (source,revision,self.cfg.audit_channel)).fetchone()
        if not row:return
        body=json.loads(row['body'])
        old_status=body['actions'].get(label,'')
        if only_pending and old_status != '等待执行' and not old_status.startswith('失败，'): return
        body['actions'][label]=status
        self.store.db.execute('UPDATE channel_reports SET body=?,version=version+1,due=0,attempts=0,failed=0 WHERE id=?',
                              (json.dumps(body,ensure_ascii=False),row['id']))
        self.store.db.commit()

    async def deliver_once(self):
        row=self.store.db.execute('''SELECT * FROM channel_reports
            WHERE destination=? AND sent_version<version AND failed=0 AND due<=?
            ORDER BY id LIMIT 1''',(self.cfg.audit_channel,time.time())).fetchone()
        if row is None:return False
        data=json.loads(row['body'])
        try:
            mid=await self.tg.publish_report(row['destination'],render_report(data,row['stale']),
                                              row['random_id'],row['message_id'])
        except Exception as exc:
            attempts=row['attempts']+1
            delay=min(self.cfg.retry_seconds*2**min(attempts-1,6),3600)
            if isinstance(exc,errors.FloodWaitError):delay=max(delay,exc.seconds+1)
            self.store.db.execute('''UPDATE channel_reports SET attempts=?,due=?,failed=?,error=?
                WHERE id=? AND version=?''',(attempts,time.time()+delay,attempts>=self.cfg.max_attempts,
                                             type(exc).__name__,row['id'],row['version']))
            self.store.db.commit()
            LOG.error('channel_log id=%s attempt=%s error=%s',row['id'],attempts,type(exc).__name__)
        else:
            # Keep a concurrently updated version dirty, but retain the sent message ID.
            self.store.db.execute('UPDATE channel_reports SET message_id=?,sent_version=? WHERE id=?',
                                  (mid,row['version'],row['id']))
            self.store.db.commit()
            LOG.info('channel_log id=%s message=%s version=%s',row['id'],mid,row['version'])
        return True

    async def worker(self):
        while True:
            await self.deliver_once()
            await asyncio.sleep(1)
