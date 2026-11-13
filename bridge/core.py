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


@dataclass
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv('BRIDGE_DATA_DIR', '/data')))
    token: str = field(default_factory=lambda: os.getenv('BRIDGE_API_TOKEN', ''))
    max_file: int = field(default_factory=lambda: int(os.getenv('BRIDGE_MAX_FILE_BYTES', 20*1024*1024)))
    max_request: int = field(default_factory=lambda: int(os.getenv('BRIDGE_MAX_REQUEST_BYTES', 21*1024*1024)))
    max_media: int = field(default_factory=lambda: int(os.getenv('BRIDGE_MAX_MEDIA_BYTES', 256*1024*1024)))
    media_ttl: int = field(default_factory=lambda: int(os.getenv('BRIDGE_MEDIA_TTL_SECONDS', 86400)))
    windows: dict = field(default_factory=lambda: {s: int(os.getenv('BRIDGE_REPLY_WINDOW_'+s.upper(), 240)) for s in ('c2c','group','guild','dm')})
    authorized: set = field(default_factory=lambda: set(filter(None, os.getenv('BRIDGE_PROACTIVE_SESSIONS','').split(','))))
    send_interval: float = field(default_factory=lambda: float(os.getenv('BRIDGE_SEND_INTERVAL_SECONDS', '1')))
    max_replies: int = field(default_factory=lambda: int(os.getenv('BRIDGE_MAX_REPLIES_PER_MESSAGE', '1')))


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
        item.setdefault('content', '')
        item.setdefault('attachments', [])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO messages(message_id,scene,chat_id,payload) VALUES(?,?,?,?)',
                (item['message_id'],item['scene'],item['chat_id'],json.dumps(item)))
        return self.db.execute('SELECT seq FROM messages WHERE scene=? AND chat_id=? AND message_id=?',
            (item['scene'], item['chat_id'],item['message_id'])).fetchone()[0]

    def message(self, seq):
        row = self.db.execute('SELECT * FROM messages WHERE seq=?',(seq,)).fetchone()
        if row is None:
            raise HTTPException(404, 'message not found')
        return {**json.loads(row['payload']), 'seq':row['seq'], 'acked':bool(row['acked'])}

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
        return {'idempotency_key':key, **{k:row[k] for k in ('status','created_at','updated_at','error')},
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
    media_kind: Literal['image','voice','video','file'] | None = None
    markdown: bool = False

    @model_validator(mode='after')
    def valid(self):
        if bool(self.media_id) != bool(self.media_kind):
            raise ValueError('media_id and media_kind must be provided together')
        if not self.text.strip() and not self.media_id:
            raise ValueError('empty message')
        if self.markdown and self.media_id:
            raise ValueError('markdown with media is unsupported')
        return self


class Rejected(Exception):
    """Definitive platform rejection (no automatic retry)."""


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
        if not scene or not chat_id or f'{scene}:{chat_id}' not in store.cfg.authorized:
            raise HTTPException(403, 'proactive session not authorized')
    if body.get('media_id'):
        if scene not in ('c2c','group'):
            raise HTTPException(422, 'media supports c2c/group only')
        store.media_path(body['media_id'])
    if body.get('markdown') and scene in ('guild','dm'):
        raise HTTPException(422, 'guild/dm markdown unsupported')
    return scene, chat_id, reply_id


def create_app(cfg=None, backend=None):
    cfg = cfg or Settings()
    if not cfg.token or len(cfg.token)<16:
        raise RuntimeError('BRIDGE_API_TOKEN must contain at least 16 characters')
    if min(cfg.max_file,cfg.max_request,cfg.max_media,cfg.media_ttl,cfg.max_replies)<=0 or cfg.send_interval<0 or any(v<=0 for v in cfg.windows.values()):
        raise RuntimeError('invalid bridge limits')
    store = Store(cfg)
    if backend is None:
        from .sdk import OfficialBackend
        backend = OfficialBackend(store)

    async def worker():
        last_send = 0.0
        while True:
            try:
                store.cleanup()
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
                    result = await backend.send(body, route)
                    status = 'sent'
                except Rejected as exc:
                    store.report_error('send', exc)
                    status, error = 'failed', 'platform rejected request'
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
                'reconnecting':backend.reconnecting,'sends':counts,'last_error':store.last_error}

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
        return {'items':[dict(r) for r in rows]}

    @api.get('/sessions/{scene}/{chat_id}/messages')
    async def history(scene: str,chat_id: str,after: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=200)):
        return store.messages(after,limit,scene,chat_id)

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
        row = store.db.execute('SELECT fingerprint FROM sends WHERE key=?',(body.idempotency_key,)).fetchone()
        if row:
            if row[0]!=fp:
                raise HTTPException(409,'idempotency key already used for different request')
            return JSONResponse(store.send_record(body.idempotency_key),202)
        validate_route(store,payload)
        if body.reply_to:
            # Include unknown/failed reservations conservatively; explicit manual review is required.
            count = sum(1 for r in store.db.execute('SELECT payload FROM sends') if json.loads(r[0]).get('reply_to')==body.reply_to)
            if count>=cfg.max_replies:
                raise HTTPException(409,'reply quota reserved or exhausted')
        now = iso()
        with store.db:
            store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',
                (body.idempotency_key,fp,encoded,'queued',now,now,None,None))
        return JSONResponse(store.send_record(body.idempotency_key),202)

    @api.get('/sends/{key}')
    async def send_status(key: str):
        return store.send_record(key)

    app.include_router(api)
    return app
