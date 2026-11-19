"""Adapter audited against qqbot-agent-sdk 1.2.2; no SDK source modifications."""
import asyncio
from dataclasses import asdict, is_dataclass
import os
import logging
from importlib.metadata import version
import re
import time

import httpx
from fastapi import HTTPException
from qqbot_agent_sdk import EventParser, InboundEvent, QQApiClient, QQWebSocket, WSCallbacks
from qqbot_agent_sdk import MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO, MEDIA_TYPE_VOICE, MEDIA_TYPE_FILE
from qqbot_agent_sdk.api_client import Route
from qqbot_agent_sdk.dto import MessageToCreate, GuildMessageToCreate, MediaInfo, MarkdownContent, MessageReference
from qqbot_agent_sdk.media_loader import MediaUploader
from .core import Rejected, StageFailure
from .views import avatar_url
from .quotes import parse_reference_metadata, normalize_inbound_quote, sanitize_elements
from .uploader import UploadError, download_media, upload_by_url, upload_local_copy
from .media_url import validate_public_media_url


def safe_error_codes(exc):
    # Only numeric metadata is extracted; never keep SDK strings or signed URLs.
    text = str(exc)
    result = {}
    match = re.search(r'QQ Bot API error \[(\d{3})\]',text)
    if match:
        result['status_code'] = int(match.group(1))
    match = re.search(r'(?:biz_code|bizcode|code)[\s:=\"\']+(\d{1,12})(?!\d)',text)
    if match:
        result['bizcode'] = int(match.group(1))
    return result


def guard_sdk():
    if version('qqbot-agent-sdk') != '1.2.2':
        raise RuntimeError('qqbot-agent-sdk version must be 1.2.2')


def suppress_unsafe_logs():
    # SDK and HTTP libraries can log bodies, URLs, tokens, session IDs and exceptions.
    # Suppress at the emitting logger, including children (parent filters do not suffice).
    for name in ('qqbot_agent_sdk','httpx','httpcore','aiohttp'):
        logger = logging.getLogger(name)
        logger.disabled = True
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and name.startswith(('qqbot_agent_sdk.', 'httpx.', 'httpcore.', 'aiohttp.')):
            logger.disabled = True


def json_safe(value):
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k):json_safe(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str,int,float,bool)):
        return value
    raise TypeError('unsupported inbound value')


def parse_full(event_type, msg, content, raw):
    if not msg.group_openid:
        return None
    return InboundEvent(event_type=event_type,chat_id=msg.group_openid,
        user_id=msg.author.member_openid,chat_scope='group',content=content,
        message_id=msg.id,timestamp=msg.timestamp,message_type=msg.message_type,
        attachments=msg.attachments,msg_elements=msg.msg_elements,raw=raw)


class BridgeWebSocket(QQWebSocket):
    def _handle_dispatch(self, event_type, data):
        # QQ's group receive settings decide which group events arrive.
        if event_type in ('C2C_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE',
                          'MESSAGE_CREATE', 'AT_MESSAGE_CREATE', 'DIRECT_MESSAGE_CREATE'):
            # DB uniqueness is authoritative; SDK in-memory dedup must not discard failed persistence.
            self._dispatch_to_main(self._cb.on_message_event(event_type,data))
            return
        super()._handle_dispatch(event_type,data)

    async def _handle_ws_error(self, exc, backoff_idx, notify_disconnect=True):
        if self.error_callback:
            self.error_callback('gateway', exc, getattr(exc, 'code', None))
        return await super()._handle_ws_error(exc, backoff_idx, notify_disconnect)

    error_callback = None

    async def _reconnect(self, backoff_idx):
        if backoff_idx >= 5:
            raise RuntimeError('bounded reconnect cycle exhausted')
        return await super()._reconnect(backoff_idx)


class OfficialBackend:
    def __init__(self, store):
        guard_sdk()
        suppress_unsafe_logs()
        self.store = store
        self.connected = self.api_ready = False
        self.reconnecting = True
        self.ws = self.http = self.api = None
        self.app_id = os.getenv('QQBOT_APP_ID','')
        self.session = store.get_session(self.app_id)
        self.last_seen = time.monotonic()
        self.bot_ids = set()
        self._inbound_tasks = set()
        self._session_tasks = set()
        self._background_tasks = set()

    async def on_message(self, event_type, raw):
        task = asyncio.current_task()
        self._inbound_tasks.add(task)
        try:
            await self._observe_message(event_type, raw)
        except Exception as exc:
            self.store.report_error('inbound', exc)
        finally:
            self._inbound_tasks.discard(task)

    async def _observe_message(self, event_type, raw):
        if not isinstance(raw, dict) or (raw.get('author') or {}).get('bot'):
            return
        if event_type == 'GROUP_MESSAGE_CREATE':
            from qqbot_agent_sdk.dto import parse_message
            msg = parse_message(raw)
            event = parse_full(event_type, msg, msg.content.strip(), raw)
        else:
            event = EventParser.parse(event_type,raw)
        if event is None or not event.message_id or event.user_id in self.bot_ids:
            return
        attachments = []
        for att in event.attachments or []:
            item = {'filename':att.filename or '', 'content_type':att.content_type or '',
                    'url':att.resolved_url or '', 'size':att.size or 0}
            for dimension in ('width','height'):
                value = getattr(att,dimension,None)
                if type(value) is int:
                    item[dimension] = value
            if getattr(att,'asr_refer_text',None):
                item['asr_text'] = att.asr_refer_text
            if att.voice_wav_url:
                item['voice_wav_url'] = att.voice_wav_url
            attachments.append(item)
        # Platform ASR only, no downloads/STT calls.
        asr = raw.get('asr_refer_text')
        if asr is None:
            asr = next((a.get('asr_refer_text') for a in raw.get('attachments',[]) if a.get('asr_refer_text')), None)
        author = raw.get('author') or {}
        sender = self.store.identity(author,event.user_id)
        if not sender.get('username') and isinstance(getattr(event,'user_name',None),str):
            sender['username'] = event.user_name
            sender['username_source'] = 'sdk_event'
        elif 'username' in sender:
            sender['username_source'] = 'raw_event'
        mentions = []
        for mention in raw.get('mentions') or []:
            if isinstance(mention,dict):
                uid = next((mention[k] for k in ('user_openid','member_openid','id') if mention.get(k)), '')
                mentions.append({**self.store.identity(mention,uid),'source':'raw_event'})
        sdk_mentions = getattr(event,'mentions',None)
        if not mentions and sdk_mentions:
            for mention in json_safe(sdk_mentions):
                if isinstance(mention,dict):
                    uid = next((mention[k] for k in ('user_id','user_openid','member_openid','id') if mention.get(k)), '')
                    mentions.append({**self.store.identity(mention,uid),'source':'sdk_event'})
        if event.chat_scope == 'group':
            task = asyncio.create_task(self._load_group_info(event.chat_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_done)
        own_index, _ = parse_reference_metadata(raw)
        elements = sanitize_elements(raw['msg_elements'] if 'msg_elements' in raw else json_safe(event.msg_elements))
        quote = normalize_inbound_quote(raw)
        if quote is not None and 'msg_elements' not in raw:
            quote['elements'] = elements
        reference = raw.get('message_reference')
        reference_id = reference.get('message_id') if isinstance(reference, dict) else None
        safe_reference = {'message_id':str(reference_id)} if isinstance(reference_id, str) or type(reference_id) is int else None
        item = {'sender':sender,'mentions':mentions,'message_id':event.message_id,'scene':event.chat_scope,
            'chat_id':event.chat_id,'user_id':event.user_id,'content':event.content,
            'timestamp':event.timestamp,'attachments':attachments,'asr_text':asr,
            'msg_elements':elements,'message_reference':safe_reference,
            'message_type':getattr(event, 'message_type', 0) if type(getattr(event, 'message_type', 0)) is int else 0,
            'reference_index':own_index,'event_type':event_type}
        if quote is not None:
            item['quote'] = quote
        await self._store_call(self.store.observe, item)

    async def _load_group_info(self, chat_id):
        """Fetch the group name once per conversation.

        The endpoint is documented as whitelist-only, so a rejection is cached as
        unavailable instead of being retried on every message.
        """
        if not self.api or await self._store_call(self.store.group_info, chat_id):
            return
        try:
            data = await asyncio.wait_for(
                self.api.request(Route('GET','/v2/groups/{group_id}/info',group_id=chat_id)),15)
        except Exception as exc:
            self.store.report_error('group_info', exc)
            await self._store_call(self.store.set_group_info, chat_id, {'unavailable': True})
            return
        if not isinstance(data, dict):
            await self._store_call(self.store.set_group_info, chat_id, {'unavailable': True})
            return
        info = {k:data.get(k) for k in ('group_name','group_finger_memo','group_class_text','group_tags','group_member_num') if data.get(k) is not None}
        # Accept an explicit URL if present in a group-info response. The
        # documented response does not currently promise an avatar field.
        for key in ('group_avatar_url', 'group_avatar', 'avatar'):
            if avatar := avatar_url(data.get(key)):
                info['avatar'] = avatar
                break
        await self._store_call(self.store.set_group_info, chat_id, info or {'unavailable': True})

    def ready(self, ready):
        user = getattr(ready,'user',None)
        if user:
            self.bot_ids.update(str(v) for v in (getattr(user,'id',''),getattr(user,'user_openid','')) if v)
        self.online()

    def online(self):
        self.connected, self.reconnecting = True, False
        self.last_seen = time.monotonic()

    def offline(self):
        self.connected, self.reconnecting = False, True

    async def _store_call(self, function, *args):
        if hasattr(self.store, 'call'):
            return await self.store.call(function, *args)
        return function(*args)

    def _background_done(self, task):
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.store.report_error('group_info', task.exception())

    def _session_done(self, task):
        self._session_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.store.report_error('session', task.exception())

    def session_set(self, sid, seq):
        # SDK advances seq before dispatch/persistence. Resume survives restart, but this
        # is NOT an atomic event+cursor commit and cannot guarantee no lost events.
        self.session = (sid,seq)
        if getattr(self.store, 'remote', False):
            task = asyncio.create_task(self._store_call(self.store.set_session, self.app_id, sid, seq))
            self._session_tasks.add(task)
            task.add_done_callback(self._session_done)
        else:
            self.store.set_session(self.app_id, sid, seq)

    def fatal(self, code, message):
        self.store.report_error('gateway_fatal')
        self.offline()

    async def on_interaction(self, event_type, raw):
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id']:
            return
        try:
            await self.api.acknowledge_interaction(raw['id'], code=0)
        except Exception as exc:
            self.store.report_error('interaction_ack', exc)

    def callbacks(self):
        return WSCallbacks(on_message_event=self.on_message,
            on_connected=self.online,on_disconnected=self.offline,
            on_fatal_error=self.fatal,get_token=self.api.ensure_token_sync,
            get_session=lambda:self.session,set_session=self.session_set,
            set_heartbeat_interval=lambda value:None,clear_token=self.api.clear_token,
            fail_pending=lambda reason:None,get_gateway_url=self.api.get_gateway_url_sync,
            on_interaction_event=self.on_interaction,on_ready=self.ready,
            on_heartbeat_ack=lambda:setattr(self,'last_seen',time.monotonic()))

    async def run(self):
        app_id, secret = os.getenv('QQBOT_APP_ID',''),os.getenv('QQBOT_APP_SECRET','')
        if not app_id or not secret:
            self.reconnecting = False
            return  # liveness/HTTP remains available; status is not ready
        self.http = httpx.AsyncClient(timeout=30,follow_redirects=False)
        self.api = QQApiClient(app_id=app_id,client_secret=secret,log_tag='QQBridge')
        self.api.setup(self.http)
        delay = 1
        while True:
            runner = None
            stage = 'token'
            try:
                await asyncio.wait_for(self.api.ensure_token(),45)
                stage = 'gateway_url'
                url = await asyncio.wait_for(self.api.get_gateway_url(),45)
                self.api_ready = True
                stage = 'gateway'
                callbacks = self.callbacks()
                self.ws = BridgeWebSocket(callbacks,log_tag='QQBridge')
                self.ws.error_callback = self.store.report_error
                # Same event loop avoids cross-thread SQLite/lifecycle races. SDK low-level runner
                # creates/closes aiohttp session and handles its own bounded reconnect attempts.
                self.ws._main_loop = asyncio.get_running_loop()
                runner = asyncio.create_task(self.ws._run_ws_async(url))
                self.last_seen = time.monotonic()
                while not runner.done():
                    await asyncio.sleep(1)
                    if time.monotonic()-self.last_seen > 180:
                        raise RuntimeError('gateway heartbeat stale')
                    if self.connected:
                        delay = 1
                await runner
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.report_error(stage, exc, getattr(exc, 'code', None))
            finally:
                self.offline()
                self.api_ready = False
                if self.ws:
                    await self.ws.stop()
                if runner:
                    runner.cancel()
                    await asyncio.gather(runner,return_exceptions=True)
            await asyncio.sleep(delay)
            delay = min(60,delay*2)

    async def send(self, body, route):
        scene, chat_id, reply_id = route
        # Guild DTO in 1.2.2 has no Markdown field; reject rather than silently change format.
        if scene in ('guild','dm') and body['markdown']:
            raise Rejected('guild/dm markdown unavailable')
        key = body.get('idempotency_key')
        async def stage(value):
            if key:
                await self._store_call(self.store.set_send_stage, key,value)
        async def quote_index():
            from .core import resolve_quote
            try:
                return await self._store_call(resolve_quote, self.store, body, scene, chat_id)
            except HTTPException:
                raise StageFailure('validate', reason='quote target unavailable or invalid') from None
        # Fail before any upload; resolve again immediately before sending because
        # media upload may take time and a stored target may have changed.
        await quote_index()
        msg_seq = body.get('_msg_seq') or (1 if reply_id else QQApiClient.next_msg_seq())
        if body.get('media_id') or body.get('media_url'):
            await stage('validate')
            try:
                if body.get('media_url'):
                    source = await validate_public_media_url(body['media_url'], self.store.cfg.media_url_hosts)
                    filename = self.store.safe_filename(body.get('filename')) if body.get('filename') else None
                else:
                    source = str((await self._store_call(self.store.media_path, body['media_id'])).resolve())
                    filename = self.store.safe_filename(body['filename']) if body.get('filename') else await self._store_call(self.store.media_filename, body['media_id'],body['media_kind'])
            except Exception:
                raise StageFailure('validate') from None
            await stage('upload')
            kinds = {'image':MEDIA_TYPE_IMAGE,'video':MEDIA_TYPE_VIDEO,'voice':MEDIA_TYPE_VOICE,'file':MEDIA_TYPE_FILE}
            file_type = kinds[body['media_kind']]
            token = await self._upload_media(scene,chat_id,source,file_type,filename,is_url=bool(body.get('media_url')))
            await stage('validate')
            from .core import validate_route
            try:
                await self._store_call(validate_route, self.store,body)
            except HTTPException:
                raise StageFailure('validate') from None
            msg = MessageToCreate(content=body['text'],msg_type=7,msg_seq=msg_seq,msg_id=reply_id or '',media=MediaInfo(file_info=token))
        else:
            msg = MessageToCreate(content=body['text'],msg_type=0,msg_seq=msg_seq,msg_id=reply_id or '')
            if body['markdown']:
                msg.msg_type, msg.content = 2, ''
                msg.markdown = MarkdownContent(content=body['text'])
        index = await quote_index()
        if index is not None:
            msg.message_reference = MessageReference(message_id=index)
        await stage('send')
        try:
            if scene=='c2c':
                return await self.api.post_c2c_message(chat_id,msg)
            if scene=='group':
                return await self.api.post_group_message(chat_id,msg)
            guild_msg = GuildMessageToCreate(content=body['text'],msg_id=reply_id or '')
            if scene=='guild':
                return await self.api.post_guild_message(chat_id,guild_msg)
            return await self.api.request(Route('POST','/dms/{guild_id}/messages',guild_id=chat_id),guild_msg.to_dict())
        except Exception as exc:
            codes = safe_error_codes(exc)
            status = codes.get('status_code')
            definite = status is not None and 400 <= status < 500 and status != 408
            raise StageFailure('send',**codes,uncertain=not definite) from None

    async def _upload_media(self, scene, chat_id, source, file_type, filename, is_url):
        """Upload via the platform URL path, falling back to a local copy.

        The platform reports "cannot download this URL" (850026) separately from
        a bad format, so a failed fetch is retried once with the bytes fetched by
        the bridge and uploaded through the chunked path.
        """
        if not is_url:
            uploader = MediaUploader(self.api,self.http,log_tag='QQBridge')
            try:
                return await asyncio.wait_for(uploader.upload(scene,chat_id,source,file_type,file_name=filename),30)
            except Exception as exc:
                raise StageFailure('upload',**safe_error_codes(exc)) from None
        try:
            return await upload_by_url(self.api,self.http,scene,chat_id,source,file_type,filename)
        except UploadError as exc:
            if not exc.can_retry_locally:
                raise StageFailure('upload',status_code=exc.status_code,bizcode=exc.bizcode,reason=exc.reason) from None
            first = exc
        try:
            data = await download_media(self.http,source,self.store.cfg.max_file)
            return await asyncio.wait_for(
                upload_local_copy(self.api,self.http,scene,chat_id,data,file_type,filename or 'media'),120)
        except Exception:
            reason = first.reason or 'QQ could not download the media URL'
            raise StageFailure('upload',status_code=first.status_code,bizcode=first.bizcode,
                               reason=reason+' and the bridge could not fetch it either') from None

    async def close(self):
        if self.ws:
            await self.ws.stop()
        for task in list(self._inbound_tasks):
            task.cancel()
        await asyncio.gather(*list(self._inbound_tasks), return_exceptions=True)
        for task in list(self._background_tasks):
            task.cancel()
        await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
        await asyncio.gather(*list(self._session_tasks), return_exceptions=True)
        if self.http:
            await self.http.aclose()
        self.connected = self.api_ready = self.reconnecting = False
