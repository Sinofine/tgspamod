from dataclasses import dataclass, field
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv


@dataclass
class Config:
    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)
    chats: set[int]
    llm_url: str
    llm_key: str = field(repr=False)
    llm_model: str
    audit_channel: str = ''
    audit_channel_mode: str = 'flagged'
    chat_references: dict[int, str] = field(default_factory=dict)
    data_dir: Path = Path('data')
    dry_run: bool = False
    kick_mode: str = 'kick'
    threshold: float = 0.9
    llm_timeout: float = 30
    llm_json_mode: bool = True
    llm_extra: dict = field(default_factory=dict)
    llm_concurrency: int = 2
    max_attempts: int = 8
    retry_seconds: float = 10
    check_unseen: bool = False
    restrict_newcomer_media: bool = True
    exempt: set[int] = field(default_factory=set)
    group_policy: str = '禁止商业推广、拉客、诈骗和导流广告；允许正常讨论、求助、新闻引用和非推广性质的链接分享。'
    tg_proxy: dict | None = field(default=None, repr=False)
    llm_proxy: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path='.env'):
        load_dotenv(path, override=False, interpolate=False)
        def required(key):
            value = os.environ.get(key, '').strip()
            if not value or value.startswith('replace_'):
                raise ValueError(f'请在 .env 中填写 {key}')
            return value
        def boolean(key, default=False):
            value = os.environ.get(key, str(default)).lower().strip()
            if value not in ('true', 'false', '1', '0'):
                raise ValueError(f'{key} 必须是 true/false')
            return value in ('true', '1')
        def ids(key, required_value=False):
            raw = required(key) if required_value else os.environ.get(key, '')
            return {int(v.strip()) for v in raw.split(',') if v.strip()}
        proxy = None
        raw_proxy = os.environ.get('TG_PROXY_URL', '').strip()
        if raw_proxy:
            p = urlparse(raw_proxy)
            if p.scheme not in ('socks5', 'socks4', 'http') or not p.hostname or not p.port:
                raise ValueError('TG_PROXY_URL 需要 socks5://host:port 或 http://host:port')
            proxy = dict(proxy_type=p.scheme, addr=p.hostname, port=p.port, rdns=True,
                         username=unquote(p.username) if p.username else None,
                         password=unquote(p.password) if p.password else None)
        cfg = cls(
            api_id=int(required('TG_API_ID')), api_hash=required('TG_API_HASH'),
            bot_token=required('TG_BOT_TOKEN'), chats=ids('TG_CHAT_IDS', True),
            llm_url=required('LLM_BASE_URL').rstrip('/'), llm_key=required('LLM_API_KEY'),
            llm_model=required('LLM_MODEL'), data_dir=Path(os.environ.get('DATA_DIR', 'data')),
            audit_channel=os.environ.get('AUDIT_CHANNEL_ID','').strip(),
            audit_channel_mode=os.environ.get('AUDIT_CHANNEL_MODE','flagged').strip(),
            dry_run=boolean('DRY_RUN'), kick_mode=os.environ.get('KICK_MODE', 'kick'),
            threshold=float(os.environ.get('AD_CONFIDENCE_THRESHOLD', '0.90')),
            llm_timeout=float(os.environ.get('LLM_TIMEOUT_SECONDS', '30')),
            llm_json_mode=boolean('LLM_JSON_MODE', True),
            llm_extra=json.loads(os.environ.get('LLM_EXTRA_BODY', '{}')),
            llm_concurrency=int(os.environ.get('LLM_CONCURRENCY', '2')),
            max_attempts=int(os.environ.get('JOB_MAX_ATTEMPTS', '8')),
            retry_seconds=float(os.environ.get('JOB_RETRY_SECONDS', '10')),
            check_unseen=boolean('CHECK_UNSEEN_MEMBERS'), exempt=ids('EXEMPT_USER_IDS'),
            restrict_newcomer_media=boolean('RESTRICT_NEWCOMER_MEDIA', True),
            group_policy=os.environ.get('GROUP_POLICY', cls.group_policy), tg_proxy=proxy,
            llm_proxy=os.environ.get('LLM_PROXY_URL') or None)
        if cfg.audit_channel_mode not in ('flagged','all'):
            raise ValueError('AUDIT_CHANNEL_MODE 必须为 flagged 或 all')
        if cfg.audit_channel:
            if not re.fullmatch(r'(?:-100[0-9]+|@[A-Za-z][A-Za-z0-9_]{3,31})',cfg.audit_channel):
                raise ValueError('AUDIT_CHANNEL_ID 必须为频道 -100… ID 或 @用户名')
            if cfg.audit_channel.lstrip('-').isdigit() and int(cfg.audit_channel) in cfg.chats:
                raise ValueError('审核频道不能同时作为受审群')
        references = json.loads(os.environ.get('TG_CHAT_REFERENCES', '{}'))
        if not isinstance(references, dict):
            raise ValueError('TG_CHAT_REFERENCES 必须是群 ID 到 @公开群用户名的 JSON 对象')
        for key, reference in references.items():
            try:
                chat = int(key)
            except (ValueError, TypeError):
                raise ValueError('TG_CHAT_REFERENCES 的键必须是数字群 ID') from None
            if chat not in cfg.chats or not isinstance(reference, str) or not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}', reference):
                raise ValueError('TG_CHAT_REFERENCES 只接受 TG_CHAT_IDS 中的群 ID 和 @公开群用户名')
            cfg.chat_references[chat] = reference
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', cfg.bot_token):
            raise ValueError('TG_BOT_TOKEN 格式不正确')
        if cfg.api_id <= 0 or not cfg.chats or any(c >= 0 for c in cfg.chats):
            raise ValueError('TG_API_ID 必须为正数，TG_CHAT_IDS 必须为负数群 ID')
        if cfg.kick_mode not in ('kick', 'ban') or not 0 <= cfg.threshold <= 1:
            raise ValueError('检查 KICK_MODE 和 AD_CONFIDENCE_THRESHOLD')
        if min(cfg.llm_timeout, cfg.llm_concurrency, cfg.max_attempts, cfg.retry_seconds) <= 0:
            raise ValueError('超时、并发数、重试次数、重试间隔必须大于 0')
        p = urlparse(cfg.llm_url)
        if p.scheme not in ('http', 'https') or not p.netloc or p.query or p.fragment or p.username:
            raise ValueError('LLM_BASE_URL 必须为不带认证/查询参数的 HTTP(S) API 根地址')
        if cfg.llm_url.endswith('/chat/completions'):
            raise ValueError('LLM_BASE_URL 请填 API 根地址（例如 /v1），不包含 /chat/completions')
        protected = {'messages', 'model', 'stream', 'tools', 'tool_choice', 'response_format', 'n'}
        if not isinstance(cfg.llm_extra, dict) or protected.intersection(cfg.llm_extra):
            raise ValueError('LLM_EXTRA_BODY 不能覆盖 messages/model/stream/tools/response_format/n')
        return cfg
