import asyncio
import hashlib
import json
import logging
import re
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .media_url import validate_media_url, validate_public_media_url


def iso(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).isoformat()


def epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None
        return dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


SCENES = ('c2c','group','guild','dm')
HARD_REPLIES = dict(c2c=4, group=5, guild=5, dm=5)
DEFAULT_WINDOWS = dict(c2c=3600, group=300, guild=300, dm=300)


def env_number(name, default, cast=int):
    raw = os.getenv(name, '').strip()
    try:
        value = cast(raw) if raw else default
        if value < 0 or (isinstance(value, float) and not __import__('math').isfinite(value)):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise RuntimeError('invalid '+name) from None


@dataclass
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv('BRIDGE_DATA_DIR', '/data').strip() or '/data'))
    token: str = field(default_factory=lambda: os.getenv('BRIDGE_API_TOKEN', '').strip())
    max_file: int = field(default_factory=lambda: env_number('BRIDGE_MAX_FILE_BYTES', 20*1024*1024))
    max_request: int = field(default_factory=lambda: env_number('BRIDGE_MAX_REQUEST_BYTES', 21*1024*1024))
    max_media: int = field(default_factory=lambda: env_number('BRIDGE_MAX_MEDIA_BYTES', 256*1024*1024))
    media_ttl: int = field(default_factory=lambda: env_number('BRIDGE_MEDIA_TTL_SECONDS', 86400))
    windows: dict = field(default_factory=lambda: {s: env_number('BRIDGE_REPLY_WINDOW_'+s.upper(), DEFAULT_WINDOWS[s]) for s in SCENES})
    authorized: set = field(default_factory=lambda: {v.strip() for v in os.getenv('BRIDGE_PROACTIVE_SESSIONS','').split(',') if v.strip()})
    proactive_deny: set = field(default_factory=lambda: {v.strip() for v in os.getenv('BRIDGE_PROACTIVE_DENY','').split(',') if v.strip()})
    media_url_hosts: set = field(default_factory=lambda: {v.strip().lower() for v in os.getenv('BRIDGE_MEDIA_URL_HOSTS','').split(',') if v.strip()})
    send_interval: float = field(default_factory=lambda: env_number('BRIDGE_SEND_INTERVAL_SECONDS', 1, float))
    max_replies: int | None = field(default_factory=lambda: env_number('BRIDGE_MAX_REPLIES_PER_MESSAGE', None) if os.getenv('BRIDGE_MAX_REPLIES_PER_MESSAGE','').strip() else None)
    reply_limits: dict = field(default_factory=lambda: {s: env_number('BRIDGE_MAX_REPLIES_'+s.upper(), HARD_REPLIES[s]) for s in SCENES})

    def __post_init__(self):
        for scene in SCENES:
            self.windows[scene] = min(self.windows.get(scene, DEFAULT_WINDOWS[scene]), DEFAULT_WINDOWS[scene])
            self.reply_limits[scene] = min(self.max_replies if self.max_replies is not None else self.reply_limits.get(scene,HARD_REPLIES[scene]), HARD_REPLIES[scene])
        if any(v <= 0 for v in (*self.windows.values(), *self.reply_limits.values())):
            raise RuntimeError('invalid BRIDGE_REPLY_WINDOW or BRIDGE_MAX_REPLIES limits')



class TooLarge(Exception):
    pass


class RequestLimit:
    # Buffer only up to a configured hard ceiling, before multipart parsing or side effects.
    # SpooledTemporaryFile avoids holding the entire request in RAM.
    def __init__(self, app, limit, token):
        self.app, self.limit, self.token = app, limit, token

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        import tempfile
        headers = dict(scope.get('headers', []))
        if scope.get('path', '') == '/v1' or scope.get('path', '').startswith('/v1/'):
            supplied = headers.get(b'authorization', b'')
            if not secrets.compare_digest(supplied, ('Bearer '+self.token).encode()):
                return await JSONResponse({'detail':'unauthorized'}, 401,
                    headers={'WWW-Authenticate':'Bearer'})(scope, receive, send)
        try:
            declared = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'detail':'invalid content-length'}, 400)(scope, receive, send)
        if declared > self.limit:
            return await JSONResponse({'detail':'request too large'}, 413)(scope, receive, send)
        with tempfile.SpooledTemporaryFile(max_size=1024*1024) as spool:
            total = 0
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
                body = event.get('body', b'')
                total += len(body)
                if total > self.limit:
                    return await JSONResponse({'detail':'request too large'}, 413)(scope, receive, send)
                spool.write(body)
                if not event.get('more_body', False):
                    break
            spool.seek(0)
            finished = False
            async def replay():
                nonlocal finished
                if finished:
                    return await receive()
                chunk = spool.read(65536)
                more = spool.tell() < total
                finished = not more
                return {'type':'http.request', 'body':chunk, 'more_body':more}
            await self.app(scope, replay, send)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_error = None
        self.upload_lock = asyncio.Lock()
        self.active_parts = set()
        cfg.data.mkdir(parents=True, exist_ok=True)
        self.media_dir = cfg.data / 'media'
        self.media_dir.mkdir(exist_ok=True)
        self.db = sqlite3.connect(cfg.data/'bridge.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id TEXT NOT NULL,scene TEXT NOT NULL,chat_id TEXT NOT NULL,payload TEXT NOT NULL,
          acked INTEGER DEFAULT 0, UNIQUE(scene,chat_id,message_id));
        CREATE TABLE IF NOT EXISTS sends(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
          payload TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          error TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY,size INTEGER,expires REAL);
        CREATE TABLE IF NOT EXISTS media_names(id TEXT PRIMARY KEY,filename TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata(namespace TEXT NOT NULL,key TEXT NOT NULL,
          value TEXT NOT NULL,PRIMARY KEY(namespace,key));
        """)
        self.db.execute("UPDATE sends SET status='unknown',error='process interrupted during send',updated_at=? WHERE status='sending'", (iso(),))
        self.db.commit()
        self.upgrade()

    def upgrade(self):
        # Keep legacy tables/column counts untouched. Re-running this migration is safe.
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles(scene TEXT,chat_id TEXT,user_id TEXT,payload TEXT,
          PRIMARY KEY(scene,chat_id,user_id));
        CREATE TABLE IF NOT EXISTS send_routes(key TEXT PRIMARY KEY,scene TEXT,chat_id TEXT,reply_to INTEGER);
        CREATE INDEX IF NOT EXISTS reply_reservations ON send_routes(reply_to);
        CREATE TABLE IF NOT EXISTS send_details(key TEXT PRIMARY KEY,stage TEXT,msg_seq INTEGER,error_details TEXT);
        CREATE TABLE IF NOT EXISTS history(history_seq INTEGER PRIMARY KEY AUTOINCREMENT,
          scene TEXT,chat_id TEXT,direction TEXT,inbound_seq INTEGER UNIQUE,send_key TEXT UNIQUE);
        CREATE INDEX IF NOT EXISTS session_history ON history(scene,chat_id,history_seq);
        """)
        fresh_history = not self.db.execute('SELECT 1 FROM history LIMIT 1').fetchone()
        backlog = []
        for row in self.db.execute('SELECT seq,payload FROM messages ORDER BY seq').fetchall():
            item = json.loads(row['payload'])
            if fresh_history:
                backlog.append((epoch(item.get('received_at')) or epoch(item.get('timestamp')) or 0, 0, row['seq']))
            else:
                self.index_message(row['seq'])
        for row in self.db.execute('SELECT * FROM sends ORDER BY rowid').fetchall():
            body = json.loads(row['payload'])
            scene, chat = body.get('scene'), body.get('chat_id')
            if body.get('reply_to'):
                original = self.db.execute('SELECT scene,chat_id FROM messages WHERE seq=?',(body['reply_to'],)).fetchone()
                if original:
                    scene, chat = original
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO send_routes VALUES(?,?,?,?)',(row['key'],scene,chat,body.get('reply_to')))
                ordinal = self.db.execute('SELECT COUNT(*) FROM send_routes WHERE reply_to=?',(body.get('reply_to'),)).fetchone()[0] if body.get('reply_to') else None
                self.db.execute('INSERT OR IGNORE INTO send_details VALUES(?,?,?,NULL)',(row['key'],row['status'],ordinal))
            if row['status'] in ('sent','failed','unknown'):
                if fresh_history:
                    backlog.append((epoch(row['updated_at']) or epoch(row['created_at']) or 0, 1, row['key']))
                else:
                    self.index_send(row['key'])
        for _, direction, identifier in sorted(backlog, key=lambda entry: (entry[0], entry[1], str(entry[2]))):
            if direction == 0:
                self.index_message(identifier)
            else:
                self.index_send(identifier)

    @staticmethod
    def identity(raw, user_id):
        result = {'user_id':user_id,'qq_number':None,'name_source':'official_event'}
        for key in ('username','member_role','bot','avatar'):
            value = raw.get(key)
            if (key == 'bot' and type(value) is bool) or (key != 'bot' and isinstance(value,(str,int)) and not isinstance(value,bool)):
                result[key] = value
        if raw.get('username_source') in ('raw_event','sdk_event'):
            result['username_source'] = raw['username_source']
        return result

    def proactive(self, scene, chat):
        # The QQ platform decides whether a proactive message is allowed.
        # The bridge only honours an explicit, opt-in deny list.
        return f'{scene}:{chat}' not in self.cfg.proactive_deny

    def profile(self, scene, chat, user):
        row = self.db.execute('SELECT payload FROM profiles WHERE scene=? AND chat_id=? AND user_id=?',(scene,chat,user)).fetchone()
        return json.loads(row[0]) if row else self.identity({},user)

    def index_message(self, seq):
        row = self.db.execute('SELECT * FROM messages WHERE seq=?',(seq,)).fetchone()
        item = json.loads(row['payload'])
        user = item.get('user_id','')
        sender = self.identity(item.get('sender') or {},user)
        with self.db:
            existing = self.db.execute('SELECT 1 FROM history WHERE inbound_seq=?',(seq,)).fetchone()
            if not existing and user:
                profile = self.profile(row['scene'],row['chat_id'],user)
                profile.update(sender)
                profile.update(scene=row['scene'],chat_id=row['chat_id'],updated_at=item.get('received_at') or item.get('timestamp'))
                self.db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)',(row['scene'],row['chat_id'],user,json.dumps(profile)))
            self.db.execute("INSERT OR IGNORE INTO history(scene,chat_id,direction,inbound_seq) VALUES(?,?,'in',?)",(row['scene'],row['chat_id'],seq))

    def index_send(self, key):
        route = self.db.execute('SELECT scene,chat_id FROM send_routes WHERE key=?',(key,)).fetchone()
        if route and route['scene'] and route['chat_id']:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO history(scene,chat_id,direction,send_key) VALUES(?,?,'out',?)",(*route,key))

    def reply_count(self, seq):
        return self.db.execute('SELECT COUNT(*) FROM send_routes WHERE reply_to=?',(seq,)).fetchone()[0]

    def set_send_stage(self, key, stage, error_details=None):
        if stage not in ('queued','validate','upload','send','sent','failed','unknown'):
            stage = 'send'
        safe = {k:v for k,v in (error_details or {}).items() if k in ('status_code','bizcode') and type(v) is int}
        with self.db:
            self.db.execute('UPDATE send_details SET stage=?,error_details=? WHERE key=?',(stage,json.dumps(safe) if safe else None,key))

    def session_history(self, scene, chat, after, limit, direction):
        sql = 'SELECT * FROM history WHERE scene=? AND chat_id=? AND history_seq>?'
        args = [scene,chat,after]
        if direction != 'all':
            sql += ' AND direction=?'
            args.append(direction)
        rows = self.db.execute(sql+' ORDER BY history_seq LIMIT ?',args+[limit]).fetchall()
        items = []
        for row in rows:
            if row['direction'] == 'in':
                entry = {**self.message(row['inbound_seq']),'inbound_seq':row['inbound_seq'],'kind':'message'}
            else:
                send = self.db.execute('SELECT payload,status,created_at,updated_at FROM sends WHERE key=?',(row['send_key'],)).fetchone()
                body = json.loads(send['payload'])
                entry = {k:body.get(k) for k in ('text','media_id','media_kind','filename','reply_to')}
                # History doesn't replicate signed media URLs.
                entry.update(created_at=send['created_at'],updated_at=send['updated_at'],timestamp=send['updated_at'],idempotency_key=row['send_key'],status=send['status'],delivered=send['status']=='sent',kind='message' if send['status']=='sent' else 'send_attempt',media_source='url' if body.get('media_url') else 'local' if body.get('media_id') else None)
            items.append({**entry,'history_seq':row['history_seq'],'direction':row['direction'],'scene':scene,'chat_id':chat})
        return {'items':items,'next_cursor':items[-1]['history_seq'] if items else after}

    def report_error(self, stage, exc=None, close_code=None):
        # Never retain exception text, payloads, credentials or arbitrary SDK error strings.
        allowed = {'TimeoutError','RuntimeError','ValueError','TypeError','OSError',
                   'OperationalError','IntegrityError','HTTPStatusError','ConnectError',
                   'ReadTimeout','Rejected'}
        name = type(exc).__name__ if exc is not None else 'GatewayError'
        name = name if name in allowed or name == 'GatewayError' else 'ExternalError'
        code = close_code if type(close_code) is int and 1000 <= close_code <= 4999 else None
        self.last_error = {'stage':stage,'class':name,'close_code':code}
        logging.getLogger(__name__).warning('bridge failure stage=%s class=%s close_code=%s', stage, name, code)

    def get_session(self, app_id):
        row = self.db.execute("SELECT value FROM metadata WHERE namespace=? AND key='ws_session'", (app_id,)).fetchone()
        return tuple(json.loads(row[0])) if row else (None, None)

    def set_session(self, app_id, sid, seq):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,'ws_session',?)", (app_id,json.dumps([sid,seq])))

    @staticmethod
    def safe_filename(value):
        # Display/upload metadata only; never used to construct a disk path. No format validation.
        name = str(value or '').replace('\\','/').rsplit('/',1)[-1]
        name = re.sub(r'[^A-Za-z0-9._-]', '_', name).strip(' .')
        if not name or name in ('.','..'):
            return 'upload.bin'
        stem, ext = os.path.splitext(name)
        return stem[:140] + ext[:20]

    def media_filename(self, media_id, kind):
        row = self.db.execute('SELECT filename FROM media_names WHERE id=?',(media_id,)).fetchone()
        name = self.safe_filename(row[0] if row else 'upload')
        # Names preserve the supplied extension. These defaults do not attest file contents.
        if not os.path.splitext(name)[1]:
            name += {'voice':'.silk','file':'.bin','image':'.png','video':'.mp4'}[kind]
        return name

    def media_usage(self):
        return sum(p.stat().st_size for p in self.media_dir.iterdir() if p.is_file() and not p.is_symlink())

    def observe(self, item):
        item = dict(item)
        now = time.time()
        item['received_at'] = iso(now)
        ts = epoch(item.get('timestamp'))
        window = self.cfg.windows[item['scene']]
        item['reply_deadline'] = iso(min(ts+window, now+window)) if ts is not None and ts <= now+30 else None
        item['sender'] = self.identity(item.get('sender') or {}, item.get('user_id',''))
        item.setdefault('content', '')
        item.setdefault('attachments', [])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO messages(message_id,scene,chat_id,payload) VALUES(?,?,?,?)',
                (item['message_id'],item['scene'],item['chat_id'],json.dumps(item)))
        seq = self.db.execute('SELECT seq FROM messages WHERE scene=? AND chat_id=? AND message_id=?',
            (item['scene'], item['chat_id'],item['message_id'])).fetchone()[0]
        self.index_message(seq)
        return seq

    def message(self, seq):
        row = self.db.execute('SELECT * FROM messages WHERE seq=?',(seq,)).fetchone()
        if row is None:
            raise HTTPException(404, 'message not found')
        item = {**json.loads(row['payload']), 'seq':row['seq'], 'acked':bool(row['acked'])}
        item.setdefault('sender',self.identity({},item.get('user_id','')))
        ts, received = epoch(item.get('timestamp')), epoch(item.get('received_at'))
        deadline = min(ts, received if received is not None else ts) + self.cfg.windows[item['scene']] if ts is not None and ts <= (received if received is not None else time.time())+30 else None
        remaining = max(0, deadline-time.time()) if deadline is not None else 0
        quota = max(0,self.cfg.reply_limits[item['scene']]-self.reply_count(seq))
        item.update(reply_deadline=iso(deadline) if deadline is not None else None,
                    can_reply=remaining>0 and quota>0, reply_remaining_seconds=int(remaining),
                    replies_remaining=quota, proactive_allowed=self.proactive(item['scene'],item['chat_id']),
                    sender_profile=self.profile(item['scene'],item['chat_id'],item.get('user_id','')))
        return item

    def messages(self, after, limit, scene=None, chat_id=None):
        sql, args = 'SELECT seq FROM messages WHERE seq>?', [after]
        for name, value in [('scene',scene),('chat_id',chat_id)]:
            if value is not None:
                sql += ' AND '+name+'=?'
                args.append(value)
        rows = self.db.execute(sql+' ORDER BY seq LIMIT ?', args+[limit]).fetchall()
        items = [self.message(r[0]) for r in rows]
        return {'items': items, 'next_cursor':items[-1]['seq'] if items else after}

    def send_record(self, key):
        row = self.db.execute('SELECT * FROM sends WHERE key=?',(key,)).fetchone()
        if row is None:
            raise HTTPException(404, 'send not found')
        details = self.db.execute('SELECT stage,msg_seq,error_details FROM send_details WHERE key=?',(key,)).fetchone()
        return {'stage':details['stage'] if details else None, 'msg_seq':details['msg_seq'] if details else None, 'error_details':json.loads(details['error_details']) if details and details['error_details'] else None, 'idempotency_key':key, **{k:row[k] for k in ('status','created_at','updated_at','error')},
                'result':json.loads(row['result']) if row['result'] else None}

    def media_path(self, media_id):
        row = self.db.execute('SELECT * FROM media WHERE id=?',(media_id,)).fetchone()
        if row is None or row['expires'] <= time.time():
            raise HTTPException(409, 'media missing or expired')
        # IDs are generated internally; never interpret user input as a path.
        if len(media_id)!=32 or any(c not in '0123456789abcdef' for c in media_id):
            raise HTTPException(409, 'invalid media id')
        path = self.media_dir/media_id
        if not path.is_file() or path.is_symlink():
            raise HTTPException(409, 'media unavailable')
        return path

    def cleanup(self):
        # Pin queued/sending media; unknown outcomes do not trigger another send.
        pinned = {json.loads(r[0]).get('media_id') for r in self.db.execute("SELECT payload FROM sends WHERE status IN ('queued','sending')")}
        for row in self.db.execute('SELECT id FROM media WHERE expires<=?', (time.time(),)).fetchall():
            if row[0] not in pinned:
                (self.media_dir/row[0]).unlink(missing_ok=True)
                with self.db:
                    self.db.execute('DELETE FROM media WHERE id=?',(row[0],))
                    self.db.execute('DELETE FROM media_names WHERE id=?',(row[0],))
        # Only bridge-generated uncommitted files; never arbitrary files in the data directory.
        for part in self.media_dir.glob('*.part'):
            if re.fullmatch(r'[a-f0-9]{32}\.part', part.name) and part not in self.active_parts and not part.is_symlink():
                part.unlink(missing_ok=True)


class SendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')
    reply_to: int | None = Field(default=None, gt=0)
    scene: Literal['c2c','group','guild','dm'] | None = None
    chat_id: str | None = Field(default=None, min_length=1, max_length=256)
    text: str = Field(default='', max_length=2000)
    media_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    media_url: str | None = Field(default=None, max_length=8192)
    filename: str | None = Field(default=None, max_length=256)
    media_kind: Literal['image','voice','video','file'] | None = None
    markdown: bool = False

    @model_validator(mode='after')
    def valid(self):
        if self.media_id and self.media_url:
            raise ValueError('media_id and media_url are mutually exclusive')
        if bool(self.media_id or self.media_url) != bool(self.media_kind):
            raise ValueError('media source and media_kind must be provided together')
        if self.media_url:
            validate_media_url(self.media_url)
        if not self.text.strip() and not (self.media_id or self.media_url):
            raise ValueError('empty message')
        if self.markdown and (self.media_id or self.media_url):
            raise ValueError('markdown with media is unsupported')
        return self


class StageFailure(Exception):
    def __init__(self, stage='send', status_code=None, bizcode=None, uncertain=False, reason=None):
        # Platform codes are numeric; reason is a fixed local description, never
        # arbitrary text echoed from an external response.
        super().__init__(reason or 'platform operation failed')
        self.stage = stage if stage in ('validate','upload','send') else 'send'
        self.details = {k:v for k,v in dict(status_code=status_code,bizcode=bizcode).items() if type(v) is int}
        self.uncertain = uncertain
        self.reason = reason


class Rejected(StageFailure):
    """Definitive rejection; arbitrary external text is never retained."""



def validate_route(store, body):
    if body.get('reply_to'):
        msg = store.message(body['reply_to'])
        if body.get('scene') not in (None,msg['scene']) or body.get('chat_id') not in (None,msg['chat_id']):
            raise HTTPException(409, 'reply route cannot be overridden')
        deadline = epoch(msg.get('reply_deadline'))
        if deadline is None or time.time() >= deadline:
            raise HTTPException(409, 'reply expired or original timestamp unavailable')
        scene, chat_id = msg['scene'], msg['chat_id']
        reply_id = msg['message_id']
    else:
        scene, chat_id, reply_id = body.get('scene'), body.get('chat_id'), None
        if not scene or not chat_id or not store.proactive(scene, chat_id):
            raise HTTPException(403, 'proactive target is in BRIDGE_PROACTIVE_DENY')
    if body.get('media_id') or body.get('media_url'):
        if scene not in ('c2c','group'):
            raise HTTPException(422, 'media supports c2c/group only')
        if body.get('media_id'):
            store.media_path(body['media_id'])
    if body.get('markdown') and scene in ('guild','dm'):
        raise HTTPException(422, 'guild/dm markdown unsupported')
    return scene, chat_id, reply_id


def create_app(cfg=None, backend=None):
    cfg = cfg or Settings()
    if not cfg.token or len(cfg.token)<16:
        raise RuntimeError('BRIDGE_API_TOKEN must contain at least 16 characters')
    if min(cfg.max_file,cfg.max_request,cfg.max_media,cfg.media_ttl)<=0 or cfg.send_interval<0 or any(v<=0 for v in cfg.windows.values()):
        raise RuntimeError('invalid bridge limits')
    store = Store(cfg)
    if backend is None:
        from .sdk import OfficialBackend
        backend = OfficialBackend(store)

    async def worker():
        last_send = 0.0
        last_cleanup = 0.0
        while True:
            try:
                if time.monotonic()-last_cleanup >= 30:
                    store.cleanup()
                    last_cleanup = time.monotonic()
                # Expire every queued job even if the gateway is offline.
                for queued in store.db.execute("SELECT key,payload FROM sends WHERE status='queued'").fetchall():
                    try:
                        validate_route(store,json.loads(queued['payload']))
                    except HTTPException:
                        with store.db:
                            store.db.execute("UPDATE sends SET status='failed',error='queued route expired or unavailable',updated_at=? WHERE key=?",(iso(),queued['key']))
                        store.set_send_stage(queued['key'],'validate')
                        store.index_send(queued['key'])
                row = store.db.execute("SELECT * FROM sends WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row is None or not backend.api_ready or not backend.connected:
                    await asyncio.sleep(.1)
                    continue
                body = json.loads(row['payload'])
                try:
                    route = validate_route(store, body)
                except HTTPException as exc:
                    with store.db:
                        store.db.execute("UPDATE sends SET status='failed',error=?,updated_at=? WHERE key=?",(exc.detail,iso(),row['key']))
                    continue
                # One serial worker, global spacing; never automatically retry API writes.
                await asyncio.sleep(max(0,cfg.send_interval-(time.monotonic()-last_send)))
                try:
                    route = validate_route(store, body)  # recheck expiry after rate wait
                except HTTPException as exc:
                    with store.db:
                        store.db.execute("UPDATE sends SET status='failed',error=?,updated_at=? WHERE key=?",(exc.detail,iso(),row['key']))
                    continue
                with store.db:
                    store.db.execute("UPDATE sends SET status='sending',updated_at=? WHERE key=?",(iso(),row['key']))
                status, error, result = 'unknown', None, None
                try:
                    details = store.db.execute('SELECT msg_seq FROM send_details WHERE key=?',(row['key'],)).fetchone()
                    body['_msg_seq'] = details[0] if details else None
                    store.set_send_stage(row['key'],'send')
                    result = await asyncio.wait_for(backend.send(body, route),60)
                    status = 'sent'
                except StageFailure as exc:
                    store.report_error(exc.stage, exc)
                    store.set_send_stage(row['key'],exc.stage,exc.details)
                    status = 'unknown' if exc.uncertain else 'failed'
                    error = exc.reason or 'platform operation failed'
                except asyncio.CancelledError:
                    with store.db:
                        store.db.execute("UPDATE sends SET status='unknown',error='send interrupted',updated_at=? WHERE key=?",(iso(),row['key']))
                    raise
                except Exception as exc:
                    store.report_error('send', exc)
                    error = 'send outcome uncertain; do not blindly resend'
                last_send = time.monotonic()
                with store.db:
                    store.db.execute('UPDATE sends SET status=?,error=?,result=?,updated_at=? WHERE key=?',
                        (status,error,json.dumps(result,default=str) if result is not None else None,iso(),row['key']))
                store.index_send(row['key'])
                if status == 'sent':
                    store.set_send_stage(row['key'],'sent')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                store.report_error('worker', exc)
                # Keep HTTP service alive; no payload/credential logging.
                await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker())
        gateway = asyncio.create_task(backend.run())
        try:
            yield
        finally:
            task.cancel()
            gateway.cancel()
            await asyncio.gather(task,gateway,return_exceptions=True)
            await backend.close()
            store.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        return JSONResponse({'detail':[{'loc':list(e['loc']),'type':e['type'],'msg':'invalid request field'} for e in exc.errors()]},422)

    app.add_middleware(RequestLimit, limit=cfg.max_request, token=cfg.token)
    app.state.store, app.state.backend = store, backend

    async def auth(request: Request):
        supplied = request.headers.get('authorization','')
        if not secrets.compare_digest(supplied.encode(), ('Bearer '+cfg.token).encode()):
            raise HTTPException(401,'unauthorized',headers={'WWW-Authenticate':'Bearer'})
    from fastapi import APIRouter
    api = APIRouter(prefix='/v1',dependencies=[Depends(auth)])

    @app.get('/health')
    async def health():
        return {'ok':True}

    @api.get('/status')
    async def status():
        counts = dict(store.db.execute('SELECT status,COUNT(*) FROM sends GROUP BY status').fetchall())
        return {'connected':backend.connected,'api_ready':backend.api_ready,
                'reconnecting':backend.reconnecting,'sends':counts,'last_error':store.last_error,
                'queue_depth':counts.get('queued',0),'current_send':dict(current) if (current := store.db.execute("SELECT s.key,d.stage FROM sends s LEFT JOIN send_details d ON s.key=d.key WHERE s.status='sending' LIMIT 1").fetchone()) else None,
                'proactive':True,'proactive_denied':sorted(cfg.proactive_deny)}

    @api.get('/messages')
    async def messages(after: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=200),scene: str|None=None,chat_id: str|None=None):
        return store.messages(after,limit,scene,chat_id)

    @api.post('/messages/{seq}/ack')
    async def ack(seq: int):
        store.message(seq)
        with store.db:
            store.db.execute('UPDATE messages SET acked=1 WHERE seq=?',(seq,))
        return {'seq':seq,'acked':True}

    @api.get('/sessions')
    async def sessions():
        rows = store.db.execute('SELECT scene,chat_id,MAX(seq) last_seq,COUNT(*) message_count FROM messages GROUP BY scene,chat_id ORDER BY last_seq DESC').fetchall()
        items = {(r['scene'],r['chat_id']):dict(r) for r in rows}
        for route in store.db.execute('SELECT DISTINCT scene,chat_id FROM send_routes WHERE scene IS NOT NULL AND chat_id IS NOT NULL'):
            items.setdefault(tuple(route),dict(scene=route[0],chat_id=route[1],last_seq=0,message_count=0))
        for allowed in cfg.authorized:
            if ':' in allowed:
                scene, chat = allowed.split(':',1)
                items.setdefault((scene,chat),dict(scene=scene,chat_id=chat,last_seq=0,message_count=0))
        for (scene,chat), item in items.items():
            profiles = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM profiles WHERE scene=? AND chat_id=?',(scene,chat))]
            item.update(proactive_allowed=store.proactive(scene,chat),names=[dict(user_id=p['user_id'],username=p.get('username'),name_source=p.get('name_source','official_event')) for p in profiles])
        return {'items':list(items.values())}

    @api.get('/sessions/{scene}/{chat_id}/messages')
    async def history(scene: str,chat_id: str,after: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=200)):
        return store.messages(after,limit,scene,chat_id)

    @api.get('/capabilities')
    async def capabilities():
        return {'features':{'media_url':True,'profiles':True,'session_history':True,'manual_profile_alias':False,
                            'platform_asr':True,'proactive':True},
                'scenes':{scene:{'reply_window_seconds':cfg.windows[scene],'max_replies':cfg.reply_limits[scene],
                                  'platform_max_replies':HARD_REPLIES[scene] if scene in ('c2c','group') else None,
                                  'text':True,'markdown':scene in ('c2c','group'),
                                  'media_kinds':['image','video','voice','file'] if scene in ('c2c','group') else []} for scene in SCENES},
                'limits':{'max_file_bytes':cfg.max_file,'max_request_bytes':cfg.max_request,'max_media_bytes':cfg.max_media,'max_inline_media_bytes':4*1024*1024,'media_ttl_seconds':cfg.media_ttl},
                'proactive':True,'proactive_denied':sorted(cfg.proactive_deny),
                'media_url':{'https_only':True,'allowed_hosts':sorted(cfg.media_url_hosts),'platform_fetch':True},
                'cursor_semantics':{'messages':'inbound_seq','session_history':'history_seq'}}

    @api.get('/profiles')
    async def profiles(scene: str|None=None,chat_id: str|None=None,user_id: str|None=None,
                       limit: int=Query(50,ge=1,le=200),offset: int=Query(0,ge=0)):
        sql, args = 'SELECT payload FROM profiles WHERE 1=1', []
        for key,value in (('scene',scene),('chat_id',chat_id),('user_id',user_id)):
            if value is not None:
                sql += ' AND '+key+'=?'
                args.append(value)
        items = [json.loads(r[0]) for r in store.db.execute(sql+' ORDER BY scene,chat_id,user_id LIMIT ? OFFSET ?',args+[limit,offset])]
        return {'items':items,'limit':limit,'offset':offset}

    @api.get('/profiles/{scene}/{chat_id}/{user_id}')
    async def profile(scene: str,chat_id: str,user_id: str):
        if not store.db.execute('SELECT 1 FROM profiles WHERE scene=? AND chat_id=? AND user_id=?',(scene,chat_id,user_id)).fetchone():
            raise HTTPException(404,'profile not found')
        return store.profile(scene,chat_id,user_id)

    @api.get('/sessions/{scene}/{chat_id}/history')
    async def session_history(scene: str,chat_id: str,after: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=200),direction: Literal['all','in','out']='all'):
        return store.session_history(scene,chat_id,after,limit,direction)

    @api.post('/media',status_code=201)
    async def upload(file: UploadFile=File(...)):
        # Serialize admission + writes so concurrent uploads cannot exceed the disk budget.
        async with store.upload_lock:
            store.cleanup()
            media_id = secrets.token_hex(16)
            path = store.media_dir/media_id
            part = store.media_dir/(media_id+'.part')
            size, used = 0, store.media_usage()
            store.active_parts.add(part)
            try:
                with part.open('xb') as target:
                    while chunk := await file.read(65536):
                        size += len(chunk)
                        if size>cfg.max_file:
                            raise HTTPException(413,'file too large')
                        if used+size>cfg.max_media:
                            raise HTTPException(413,'media storage limit reached')
                        target.write(chunk)
                if not size:
                    raise HTTPException(422,'empty file')
                expires = time.time()+cfg.media_ttl
                part.rename(path)
                with store.db:
                    store.db.execute('INSERT INTO media VALUES(?,?,?)',(media_id,size,expires))
                    store.db.execute('INSERT INTO media_names VALUES(?,?)',
                        (media_id,store.safe_filename(file.filename)))
                return {'media_id':media_id,'size':size,'expires_at':iso(expires)}
            except BaseException:
                part.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                raise
            finally:
                store.active_parts.discard(part)
                await file.close()

    @api.post('/messages/send')
    async def enqueue(body: SendRequest):
        payload = body.model_dump()
        encoded = json.dumps(payload,sort_keys=True,separators=(',',':'))
        fp = hashlib.sha256(encoded.encode()).hexdigest()
        row = store.db.execute('SELECT fingerprint,payload FROM sends WHERE key=?',(body.idempotency_key,)).fetchone()
        if row:
            old = json.loads(row['payload'])
            compatible = all(old.get(k)==v for k,v in payload.items()) and all(payload.get(k)==v for k,v in old.items())
            if row[0]!=fp and not compatible:
                raise HTTPException(409,'idempotency key already used for different request')
            return JSONResponse(store.send_record(body.idempotency_key),202)
        route = validate_route(store,payload)
        if body.media_url:
            try:
                await validate_public_media_url(body.media_url,cfg.media_url_hosts)
            except ValueError:
                raise HTTPException(422,'media URL validation failed') from None
        existing = store.db.execute('SELECT fingerprint,payload FROM sends WHERE key=?',(body.idempotency_key,)).fetchone()
        if existing:
            old = json.loads(existing['payload'])
            if existing['fingerprint'] != fp and not (all(old.get(k)==v for k,v in payload.items()) and all(payload.get(k)==v for k,v in old.items())):
                raise HTTPException(409,'idempotency key already used for different request')
            return JSONResponse(store.send_record(body.idempotency_key),202)
        if body.reply_to:
            # Include unknown/failed reservations conservatively; explicit manual review is required.
            count = store.reply_count(body.reply_to)
            if count>=cfg.reply_limits[route[0]]:
                raise HTTPException(409,'reply quota reserved or exhausted')
        now = iso()
        with store.db:
            store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',
                (body.idempotency_key,fp,encoded,'queued',now,now,None,None))
            store.db.execute('INSERT INTO send_routes VALUES(?,?,?,?)',(body.idempotency_key,route[0],route[1],body.reply_to))
            store.db.execute('INSERT INTO send_details VALUES(?,?,?,NULL)',(body.idempotency_key,'queued',count+1 if body.reply_to else None))
        return JSONResponse(store.send_record(body.idempotency_key),202)

    @api.get('/sends/{key}')
    async def send_status(key: str):
        return store.send_record(key)

    app.include_router(api)
    return app
