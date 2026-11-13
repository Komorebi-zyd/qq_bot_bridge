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
    values = [v.strip() for v in os.getenv(name, default).split(',') if v.strip()]
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
        instructions='读取官方网关已观察并归档的消息，不是任意 QQ 历史查询。Agent 须自行定时轮询；MCP 不会唤醒 Agent。发送须检查最终状态，不得换键盲重试。')

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
        """读取 QQ 网关连接状态与共享发送队列统计；不是送达保证。"""
        return await request('GET', '/v1/status')

    @mcp.tool()
    async def qq_get_messages(after: int = 0, limit: int = 50, scene: Scene | None = None, chat_id: str | None = None) -> dict[str, Any]:
        """分页读取已观察消息；after 排他，limit 1..200。保留真实附件链接；处理后可靠保存 next_cursor。不是任意官方聊天历史。"""
        params = {'after': after, 'limit': limit}
        params.update({k: v for k, v in {'scene': scene, 'chat_id': chat_id}.items() if v is not None})
        return await request('GET', '/v1/messages', params=params)

    @mcp.tool()
    async def qq_ack_message(seq: int) -> dict[str, Any]:
        """幂等标记消费，不删除历史、不推进客户端游标。"""
        return await request('POST', f'/v1/messages/{seq}/ack')

    @mcp.tool()
    async def qq_list_sessions() -> dict[str, Any]:
        """列出已观察到消息的会话；不是联系人或群成员目录。"""
        return await request('GET', '/v1/sessions')

    @mcp.tool()
    async def qq_get_session_messages(scene: Scene, chat_id: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
        """读取一个会话的本地归档分页，limit 1..200；不能拉取未观察的官方历史。"""
        return await request('GET', f'/v1/sessions/{scene}/{quote(chat_id, safe="")}/messages', params={'after': after, 'limit': limit})

    @mcp.tool()
    async def qq_send_message(idempotency_key: str, text: str = '', reply_to: int | None = None,
            scene: Scene | None = None, chat_id: str | None = None, media_id: str | None = None,
            media_kind: MediaKind | None = None, markdown: bool = False) -> dict[str, Any]:
        """进入同一个持久发送队列。reply_to 为已观察 seq；主动发送须预授权。queued 不等于送达；同键同正文重提安全，unknown 禁止换键盲重试。"""
        return await request('POST', '/v1/messages/send', json=dict(idempotency_key=idempotency_key,
            text=text, reply_to=reply_to, scene=scene, chat_id=chat_id, media_id=media_id,
            media_kind=media_kind, markdown=markdown))

    @mcp.tool()
    async def qq_get_send_result(key: str) -> dict[str, Any]:
        """查询 queued/sending/sent/failed/unknown；sent 仅平台接受，不保证阅读。"""
        return await request('GET', '/v1/sends/' + quote(key, safe=''))

    @mcp.tool()
    async def qq_upload_media(filename: str, content_base64: str) -> dict[str, Any]:
        """仅小文件标准 base64（解码最多 4MiB），不支持本地路径或 URL 获取。大文件用 REST POST /v1/media multipart。返回 media_id；文件名只作元数据。"""
        if len(content_base64) > 4 * ((MAX_INLINE_MEDIA + 2) // 3):
            raise ToolError('inline media exceeds 4MiB; use REST multipart upload')
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ToolError('invalid standard base64') from None
        if len(data) > MAX_INLINE_MEDIA:
            raise ToolError('inline media exceeds 4MiB; use REST multipart upload')
        return await request('POST', '/v1/media', files={'file': (filename, data, 'application/octet-stream')})

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
