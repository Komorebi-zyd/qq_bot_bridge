"""MCP transport adapters; all domain operations reuse the authenticated REST app."""
import base64
import binascii
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from .core import create_app

MAX_INLINE_MEDIA = 4 * 1024 * 1024
Scene = Literal['c2c', 'group', 'guild', 'dm']
MediaKind = Literal['image', 'voice', 'video', 'file']


class BearerAuth:
    """Authenticate every HTTP method/path except the exact liveness endpoint."""
    def __init__(self, app, token):
        self.app, self.expected = app, ('Bearer ' + token).encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['path'] != '/health':
            values = [v for k, v in scope.get('headers', []) if k == b'authorization']
            if len(values) != 1 or not secrets.compare_digest(values[0], self.expected):
                return await JSONResponse({'detail': 'unauthorized'}, 401,
                    headers={'WWW-Authenticate': 'Bearer'})(scope, receive, send)
        await self.app(scope, receive, send)


def _exact_values(name, default):
    values = [v.strip() for v in (os.getenv(name, '').strip() or default).split(',') if v.strip()]
    if not values or any('*' in v or '?' in v or '#' in v for v in values):
        raise RuntimeError(name + ' must contain explicit values, not wildcards')
    return values


def attach_mcp(app):
    cfg = app.state.store.cfg
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_exact_values('BRIDGE_ALLOWED_HOSTS', 'localhost:8080,127.0.0.1:8080,[::1]:8080'),
        allowed_origins=_exact_values('BRIDGE_ALLOWED_ORIGINS', 'http://localhost:8080,http://127.0.0.1:8080,http://[::1]:8080'),
    )
    mcp = FastMCP('QQ 云桥', stateless_http=True, json_response=True,
        streamable_http_path='/mcp', transport_security=security,
        max_request_body_size=cfg.max_request,
        instructions='通过 QQ 云桥浏览会话、识别发言人、读取图片语音等附件，并发送文字或媒体直链。支持 Agent 心跳巡查、消息回复和主动联系。')

    async def request(method, path, **kwargs):
        # ASGITransport deliberately does not start another lifespan/queue/gateway.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url='http://bridge.internal', headers={'Authorization': 'Bearer ' + cfg.token}) as client:
            response = await client.request(method, path, **kwargs)
        if response.is_error:
            # Do not echo request bodies (especially base64) or validation inputs.
            detail = response.json().get('detail')
            safe = detail if isinstance(detail, str) else 'request validation failed'
            raise ToolError(f'HTTP {response.status_code}: {safe}')
        return response.json()

    @mcp.tool()
    async def qq_status() -> dict[str, Any]:
        """查看 QQ 在线状态、发送队列统计和最近运行诊断。"""
        return await request('GET', '/v1/status')

    @mcp.tool()
    async def qq_get_messages(after: int = 0, limit: int = 50, scene: Scene | None = None, chat_id: str | None = None) -> dict[str, Any]:
        """按游标读取新消息，包含发言人资料、附件链接和回复状态。after 为上次 next_cursor，limit 为每页条数（1–200）。"""
        params = {'after': after, 'limit': limit}
        params.update({k: v for k, v in {'scene': scene, 'chat_id': chat_id}.items() if v is not None})
        return await request('GET', '/v1/messages', params=params)

    @mcp.tool()
    async def qq_ack_message(seq: int) -> dict[str, Any]:
        """将 seq 对应的消息标记为已处理，返回确认状态。"""
        return await request('POST', f'/v1/messages/{seq}/ack')

    @mcp.tool()
    async def qq_list_sessions() -> dict[str, Any]:
        """列出会话及其显示名称、消息统计与主动联系状态。"""
        return await request('GET', '/v1/sessions')

    @mcp.tool()
    async def qq_get_session_messages(scene: Scene, chat_id: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
        """分页读取指定会话收到的消息，包含发言人资料、正文和附件链接。"""
        return await request('GET', f'/v1/sessions/{scene}/{quote(chat_id, safe="")}/messages', params={'after': after, 'limit': limit})

    @mcp.tool()
    async def qq_send_message(idempotency_key: str, text: str = '', reply_to: int | None = None,
            scene: Scene | None = None, chat_id: str | None = None, media_id: str | None = None,
            media_kind: MediaKind | None = None, markdown: bool = False,
            media_url: str | None = None, filename: str | None = None) -> dict[str, Any]:
        """发送文字、图片、视频、语音或文件。回复使用 reply_to；主动联系使用 scene 和 chat_id；媒体可传 media_url 直链或已上传的 media_id。idempotency_key 标识一次发送动作。"""
        return await request('POST', '/v1/messages/send', json=dict(idempotency_key=idempotency_key,
            text=text, reply_to=reply_to, scene=scene, chat_id=chat_id, media_id=media_id,
            media_kind=media_kind, markdown=markdown, media_url=media_url, filename=filename))

    @mcp.tool()
    async def qq_get_send_result(key: str) -> dict[str, Any]:
        """按发送动作的 key 查询处理阶段、最终状态和平台返回结果。"""
        return await request('GET', '/v1/sends/' + quote(key, safe=''))

    @mcp.tool()
    async def qq_upload_media(filename: str, content_base64: str) -> dict[str, Any]:
        """上传标准 Base64 编码的小文件（解码后最多 4MiB），返回可用于发送的 media_id。公网媒体直链可直接交给 qq_send_message。"""
        if len(content_base64) > 4 * ((MAX_INLINE_MEDIA + 2) // 3):
            raise ToolError('inline media exceeds 4MiB; use REST multipart upload')
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ToolError('invalid standard base64') from None
        if len(data) > MAX_INLINE_MEDIA:
            raise ToolError('inline media exceeds 4MiB; use REST multipart upload')
        return await request('POST', '/v1/media', files={'file': (filename, data, 'application/octet-stream')})

    @mcp.tool()
    async def qq_get_capabilities() -> dict[str, Any]:
        """查看文字和富媒体能力、上传限额、各场景回复时限与主动联系配置。"""
        return await request('GET', '/v1/capabilities')

    @mcp.tool()
    async def qq_get_profiles(scene: Scene | None = None, chat_id: str | None = None,
            user_id: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """分页查看会话成员资料，包含昵称、角色及资料来源，可按场景、会话或发言人筛选。"""
        params = {'limit': limit, 'offset': offset}
        params.update({k: v for k, v in {'scene': scene, 'chat_id': chat_id, 'user_id': user_id}.items() if v is not None})
        return await request('GET', '/v1/profiles', params=params)

    @mcp.tool()
    async def qq_get_profile(scene: Scene, chat_id: str, user_id: str) -> dict[str, Any]:
        """获取指定会话中一位发言人的昵称、身份角色和最新缓存资料。"""
        return await request('GET', f'/v1/profiles/{scene}/{quote(chat_id, safe="")}/{quote(user_id, safe="")}')

    @mcp.tool()
    async def qq_get_session_history(scene: Scene, chat_id: str, after: int = 0, limit: int = 50,
            direction: Literal['all', 'in', 'out'] = 'all') -> dict[str, Any]:
        """分页浏览会话的双向交流记录，含收到的消息和机器人已发送的内容。after 使用返回的历史 next_cursor。"""
        return await request('GET', f'/v1/sessions/{scene}/{quote(chat_id, safe="")}/history',
                             params={'after': after, 'limit': limit, 'direction': direction})

    transport = mcp.streamable_http_app()
    # Merge exact ASGI routes, not Mount('/mcp'): no /mcp -> /mcp/ redirect.
    sdk_endpoint = next(r.endpoint for r in transport.routes if getattr(r, 'path', None) == '/mcp')

    class StatelessEndpoint:
        async def __call__(self, scope, receive, send):
            # Stateless servers have no server-initiated stream to offer. The SDK
            # otherwise opens an idle SSE GET; explicitly reject it per MCP spec.
            if scope['method'] == 'GET':
                return await JSONResponse({'detail': 'stateless transport has no SSE stream'}, 405,
                    headers={'Allow': 'POST, DELETE'})(scope, receive, send)
            await sdk_endpoint(scope, receive, send)

    endpoint = StatelessEndpoint()
    app.router.routes.append(Route('/mcp', endpoint=endpoint))
    app.router.routes.append(Route('/mcp/', endpoint=endpoint))
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            async with mcp.session_manager.run():
                yield

    app.router.lifespan_context = lifespan
    # Added last: reject unauthorized requests before buffering any body.
    # Existing core RequestLimit also surrounds merged MCP routes (21MiB default).
    app.add_middleware(BearerAuth, token=cfg.token)
    app.state.mcp = mcp
    return app


def create_combined_app(cfg=None, backend=None):
    return attach_mcp(create_app(cfg, backend))
