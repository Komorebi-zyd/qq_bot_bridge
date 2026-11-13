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
from qqbot_agent_sdk.dto import MessageToCreate, GuildMessageToCreate, MediaInfo, MarkdownContent
from qqbot_agent_sdk.media_loader import MediaUploader
from .core import Rejected


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
    full_messages = False

    def _handle_dispatch(self, event_type, data):
        if event_type in ('C2C_MESSAGE_CREATE','GROUP_AT_MESSAGE_CREATE','MESSAGE_CREATE','AT_MESSAGE_CREATE','DIRECT_MESSAGE_CREATE') or (event_type == 'GROUP_MESSAGE_CREATE' and self.full_messages):
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
        self.full = os.getenv('BRIDGE_GROUP_FULL_MESSAGES','false').lower()=='true'

    async def on_message(self, event_type, raw):
        try:
            await self._observe_message(event_type, raw)
        except Exception as exc:
            self.store.report_error('inbound', exc)

    async def _observe_message(self, event_type, raw):
        if not isinstance(raw, dict) or (raw.get('author') or {}).get('bot'):
            return
        if event_type == 'GROUP_MESSAGE_CREATE':
            if not self.full:
                return
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
            if att.voice_wav_url:
                item['voice_wav_url'] = att.voice_wav_url
            attachments.append(item)
        # Platform ASR only, no downloads/STT calls.
        asr = raw.get('asr_refer_text')
        if asr is None:
            asr = next((a.get('asr_refer_text') for a in raw.get('attachments',[]) if a.get('asr_refer_text')), None)
        self.store.observe({'message_id':event.message_id,'scene':event.chat_scope,
            'chat_id':event.chat_id,'user_id':event.user_id,'content':event.content,
            'timestamp':event.timestamp,'attachments':attachments,'asr_text':asr,
            'msg_elements':json_safe(event.msg_elements),'message_reference':raw.get('message_reference'),
            'event_type':event_type})

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

    def session_set(self, sid, seq):
        # SDK advances seq before dispatch/persistence. Resume survives restart, but this
        # is NOT an atomic event+cursor commit and cannot guarantee no lost events.
        self.store.set_session(self.app_id, sid, seq)
        self.session = (sid,seq)

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
                self.ws.full_messages = self.full
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
        try:
            if body.get('media_id'):
                path = self.store.media_path(body['media_id'])
                uploader = MediaUploader(self.api,self.http,log_tag='QQBridge')
                kinds = {'image':MEDIA_TYPE_IMAGE,'video':MEDIA_TYPE_VIDEO,
                         'voice':MEDIA_TYPE_VOICE,'file':MEDIA_TYPE_FILE}
                token = await uploader.upload(scene,chat_id,str(path.resolve()),kinds[body['media_kind']],
                    file_name=self.store.media_filename(body['media_id'],body['media_kind']))
                # Upload does not send; expiry must be checked again before message creation.
                from .core import validate_route
                try:
                    validate_route(self.store,body)
                except HTTPException:
                    raise Rejected("media uploaded but message expired before send") from None
                msg = MessageToCreate(content=body['text'],msg_type=7,
                    msg_seq=QQApiClient.next_msg_seq(),msg_id=reply_id or '',media=MediaInfo(file_info=token))
            else:
                msg = MessageToCreate(content=body['text'],msg_type=0,
                    msg_seq=QQApiClient.next_msg_seq(),msg_id=reply_id or '')
                if body['markdown']:
                    msg.msg_type, msg.content = 2, ''
                    msg.markdown = MarkdownContent(content=body['text'])
            if scene=='c2c':
                return await self.api.post_c2c_message(chat_id,msg)
            if scene=='group':
                return await self.api.post_group_message(chat_id,msg)
            guild_msg = GuildMessageToCreate(content=body['text'],msg_id=reply_id or '')
            if scene=='guild':
                return await self.api.post_guild_message(chat_id,guild_msg)
            return await self.api.request(Route('POST','/dms/{guild_id}/messages',guild_id=chat_id),guild_msg.to_dict())
        except RuntimeError as exc:
            # Only explicit 4xx (except request timeout) is definite. 5xx/transport/parse
            # errors can occur after acceptance, therefore they remain unknown.
            match = re.search(r'QQ Bot API error \[(4\d\d)\]',str(exc))
            if match and match.group(1)!='408':
                raise Rejected('platform rejected') from None
            raise

    async def close(self):
        if self.ws:
            await self.ws.stop()
        if self.http:
            await self.http.aclose()
        self.connected = self.api_ready = self.reconnecting = False
