"""Only fake QQ backend; real official MCP client over ephemeral loopback HTTP."""
import asyncio
import base64
import json
import socket

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bridge.core import Settings, iso
from bridge.mcp_server import MAX_INLINE_MEDIA, create_combined_app

def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k != 'reply_remaining_seconds'}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


TOKEN = 'offline-test-token-0123456789'
AUTH = {'Authorization': 'Bearer ' + TOKEN}
TOOLS = {'qq_status', 'qq_get_messages', 'qq_ack_message', 'qq_list_sessions',
         'qq_get_session_messages', 'qq_send_message', 'qq_get_send_result', 'qq_upload_media',
         'qq_get_capabilities', 'qq_get_profiles', 'qq_get_profile', 'qq_get_session_history', 'qq_get_info'}


class Fake:
    connected = api_ready = True
    reconnecting = False

    def __init__(self):
        self.calls = []
        self.closed = False

    async def run(self):
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True

    async def send(self, body, route):
        self.calls.append((body, route))
        return {'id': 'fake-only', 'ext_info': {'ref_idx': 'REFIDX_fake_sent'}}


def make_app(tmp_path, monkeypatch, host='testserver'):
    monkeypatch.setenv('BRIDGE_ALLOWED_HOSTS', host)
    monkeypatch.setenv('BRIDGE_ALLOWED_ORIGINS', 'https://agent.example')
    fake = Fake()
    app = create_combined_app(Settings(data=tmp_path, token=TOKEN, send_interval=0), fake)
    return app, fake


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE', 'OPTIONS', 'PUT'])
@pytest.mark.parametrize('path', ['/mcp', '/mcp/'])
def test_transport_auth_all_methods(tmp_path, monkeypatch, method, path):
    app, fake = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        for headers in ({}, {'Authorization': 'Bearer wrong'}):
            r = client.request(method, path, headers=headers, content=b'not-json')
            assert r.status_code == 401
            assert r.headers['www-authenticate'] == 'Bearer'
        assert client.post('/mcp?token=' + TOKEN, json={}).status_code == 401
        assert client.get('/v1/status').status_code == 401
        assert client.get('/missing').status_code == 401
    assert fake.closed


def test_limits_hosts_slashes(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    with TestClient(app, headers=AUTH) as client:
        for path in ('/mcp', '/mcp/'):
            r = client.get(path, headers={'Accept': 'text/event-stream'}, follow_redirects=False)
            assert r.status_code == 405  # stateless GET deliberately unsupported
            assert 'location' not in r.headers
        assert client.post('/mcp', content='{}', headers={'Content-Length': str(22*1024*1024)}).status_code == 413
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
        headers = {'Accept': 'application/json, text/event-stream', 'Host': 'evil.example'}
        assert client.post('/mcp', json=body, headers=headers).status_code == 421
        headers['Host'] = 'testserver'
        headers['Origin'] = 'https://evil.example'
        assert client.post('/mcp', json=body, headers=headers).status_code == 403


def test_exact_allowlist_required(tmp_path, monkeypatch):
    monkeypatch.setenv('BRIDGE_ALLOWED_HOSTS', '*.example.com')
    with pytest.raises(RuntimeError, match='explicit'):
        create_combined_app(Settings(data=tmp_path, token=TOKEN), Fake())


def test_official_client_full_parity(tmp_path, monkeypatch):
    async def scenario():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        host = f'127.0.0.1:{sock.getsockname()[1]}'
        app, fake = make_app(tmp_path, monkeypatch, host)
        attachment = {'url': 'https://cdn.example/real.png', 'content_type': 'image/jpeg',
                      'filename': 'real.jpg', 'size': 1234, 'voice_wav_url': 'https://cdn.example/voice.wav'}
        seq = app.state.store.observe(dict(message_id='incoming', scene='c2c', chat_id='user',
            user_id='user', content='hello', sender={'username':'Tester','avatar':'https://cdn.example/avatar.png'}, attachments=[attachment], timestamp=iso(),
            reference_index='REFIDX_incoming'))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(.01)
            assert server.started
            async with httpx.AsyncClient(headers=AUTH, timeout=10) as http:
                async with streamable_http_client(f'http://{host}/mcp', http_client=http) as (read, write, session_id):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert session_id() is None
                        tools = await session.list_tools()
                        assert {t.name for t in tools.tools} == TOOLS
                        negative = ('禁止', '不支持', '不能', '不得', '不是', '不删除', '不推进', '不保证')
                        for tool in tools.tools:
                            assert tool.description and len(tool.description) < 220
                            assert not any(word in tool.description for word in negative), tool.name
                        send_tool = next(t for t in tools.tools if t.name == 'qq_send_message')
                        assert {'media_url', 'filename', 'quote_to', 'quote_key'} <= set(send_tool.inputSchema['properties'])

                        async def call(name, args=None):
                            result = await session.call_tool(name, args or {})
                            assert not result.isError, result
                            # Unstructured tools return one JSON text block.
                            assert result.structuredContent is None
                            return json.loads(result.content[0].text)

                        assert await call('qq_status') == (await http.get(f'http://{host}/v1/status')).json()

                        # MCP defaults to the lite projection; full stays available.
                        messages = await call('qq_get_messages', {'after': 0, 'limit': 1, 'scene': 'c2c', 'chat_id': 'user'})
                        assert messages == (await http.get(
                            f'http://{host}/v1/messages?limit=1&scene=c2c&chat_id=user&detail=lite')).json()
                        assert messages['chat'] == {'scene': 'c2c', 'id': 'user'}
                        first = messages['items'][0]
                        assert first['text'] == 'hello' and first['from']['name'] == 'Tester'
                        assert first['attachments'][0]['kind'] == 'image'
                        assert first['attachments'][0]['url'] == attachment['url']
                        assert 'message_id' not in first and 'sender_profile' not in first
                        assert 'reply_remaining_seconds' not in first
                        assert 'avatar' not in first['from']
                        full = (await http.get(
                            f'http://{host}/v1/messages?limit=1&scene=c2c&chat_id=user&detail=full')).json()['items'][0]
                        assert full['sender_profile']['avatar'] == 'https://cdn.example/avatar.png'
                        info = await call('qq_get_info', {'scene': 'c2c', 'chat_id': 'user'})
                        assert info == (await http.get(f'http://{host}/v1/info?scene=c2c&chat_id=user')).json()
                        assert info['name'] == 'Tester' and info['avatar'] == full['sender_profile']['avatar']

                        assert await call('qq_get_session_messages', {'scene': 'c2c', 'chat_id': 'user'}) == messages
                        assert (await call('qq_list_sessions'))['items'][0]['id'] == 'user'
                        assert (await call('qq_ack_message', {'seq': seq}))['acked']
                        uploaded = await call('qq_upload_media', {'filename': 'tiny.png', 'content_base64': base64.b64encode(b'fake-image').decode()})
                        assert uploaded['size'] == 10
                        send = dict(idempotency_key='mcp-turn', text='reply', reply_to=seq,
                            media_id=uploaded['media_id'], media_kind='image')
                        assert (await call('qq_send_message', send))['key'] == 'mcp-turn'
                        for _ in range(100):
                            record = await call('qq_get_send_result', {'key': 'mcp-turn'})
                            if record['status'] == 'sent':
                                break
                            await asyncio.sleep(.02)
                        assert record['status'] == 'sent' and record['key'] == 'mcp-turn'
                        assert set(record) <= {'key','status','stage','msg_seq','error','codes'}
                        assert (await http.get(f'http://{host}/v1/sends/mcp-turn?detail=lite')).json() == record
                        assert 'result' in (await http.get(f'http://{host}/v1/sends/mcp-turn?detail=full')).json()
                        assert (await call('qq_send_message', send)) == record
                        assert len(fake.calls) == 1

                        caps = await call('qq_get_capabilities')
                        assert caps == (await http.get(f'http://{host}/v1/capabilities?detail=lite')).json()
                        assert set(caps['scenes']) <= {'c2c','group'}
                        assert caps['limits']['file_mb'] == 20.0
                        assert await call('qq_get_profiles', {'scene': 'c2c', 'chat_id': 'user'}) == (await http.get(f'http://{host}/v1/profiles?scene=c2c&chat_id=user&limit=20&offset=0')).json()
                        history = await call('qq_get_session_history', {'scene': 'c2c', 'chat_id': 'user'})
                        assert history == (await http.get(
                            f'http://{host}/v1/sessions/c2c/user/history?after=0&limit=20&direction=all&detail=lite')).json()
                        assert [x['direction'] for x in history['items']] == ['in','out']
                        profile = await call('qq_get_profile', {'scene': 'c2c', 'chat_id': 'user', 'user_id': 'user'})
                        assert profile['username'] == 'Tester'
                        import bridge.core as core
                        async def public_url(url, hosts):
                            return url
                        monkeypatch.setattr(core, 'validate_public_media_url', public_url)
                        url_send = await call('qq_send_message', {'idempotency_key':'mcp-url-video', 'reply_to':seq,
                            'media_url':'https://cdn.qq.com/test.mp4', 'media_kind':'video', 'filename':'test.mp4'})
                        assert url_send['key'] == 'mcp-url-video'
                        for _ in range(100):
                            url_result = await call('qq_get_send_result', {'key':'mcp-url-video'})
                            if url_result['status'] == 'sent':
                                break
                            await asyncio.sleep(.02)
                        assert url_result['status'] == 'sent'
                        assert len(fake.calls) == 2 and fake.calls[-1][0]['media_url'] == 'https://cdn.qq.com/test.mp4'
                        quoted = app.state.store.observe(dict(message_id='quoted-turn',scene='c2c',chat_id='user',user_id='user',
                            content='你怎么看',timestamp=iso(),reference_index='REFIDX_new',quote={'reference_index':'REFIDX_incoming',
                            'elements':[{'content':'hello','author':{'user_id':'user','username':'Tester'}}]}))
                        page = await call('qq_get_messages', {'after':seq})
                        assert page['items'][0]['quote']['text']=='hello'
                        assert page['items'][0]['quote']['seq']==seq
                        assert 'REFIDX' not in json.dumps(page)
                        quote_send=dict(idempotency_key='quoted-send',reply_to=quoted,quote_to=seq,text='针对原消息回答')
                        assert (await call('qq_send_message',quote_send))['key']=='quoted-send'
                        for _ in range(100):
                            qs=await call('qq_get_send_result',{'key':'quoted-send'})
                            if qs['status']=='sent': break
                            await asyncio.sleep(.02)
                        assert qs['status']=='sent' and fake.calls[-1][0]['quote_to']==seq
                        outgoing_quote=dict(idempotency_key='quoted-own',scene='c2c',chat_id='user',quote_key='mcp-turn',text='补充我之前的回复')
                        assert (await call('qq_send_message',outgoing_quote))['key']=='quoted-own'
                        for _ in range(100):
                            qo=await call('qq_get_send_result',{'key':'quoted-own'})
                            if qo['status']=='sent': break
                            await asyncio.sleep(.02)
                        assert qo['status']=='sent' and fake.calls[-1][0]['quote_key']=='mcp-turn'


                        from bridge.sdk import BridgeWebSocket, OfficialBackend
                        store = app.state.store
                        events = []
                        backend = OfficialBackend(store)

                        class Callbacks:
                            on_message_event = backend.on_message

                        ws = BridgeWebSocket(Callbacks())
                        ws._dispatch_to_main = lambda coro: events.append(asyncio.create_task(coro))
                        ws._handle_dispatch('GROUP_MESSAGE_CREATE', {'id':'group-profile', 'group_openid':'group',
                            'content':'hello group', 'timestamp':iso(), 'author':{'member_openid':'member',
                            'username':'Member', 'avatar':'https://cdn.example/member.png'}})
                        await asyncio.gather(*events)
                        await backend.close()
                        group_messages = await call('qq_get_session_messages', {'scene':'group', 'chat_id':'group'})
                        assert [item['text'] for item in group_messages['items']] == ['hello group']
                        store.set_group_info('group', {'group_name':'Book club', 'group_member_num':12})
                        group_info = await call('qq_get_info', {'scene':'group', 'chat_id':'group'})
                        assert group_info['name'] == 'Book club' and 'avatar' not in group_info
                        member_info = await call('qq_get_info', {'scene':'group','chat_id':'group','user_id':'member'})
                        assert member_info['avatar'] == 'https://cdn.example/member.png'
                        invalid = [
                            ('qq_get_info', {'scene':'group','chat_id':'group','user_id':'unknown'}),
                            ('qq_get_messages', {'limit': 201}),
                            ('qq_send_message', {**send, 'text': 'different'}),
                            ('qq_send_message', {'idempotency_key': 'empty', 'reply_to': seq, 'text': '   '}),
                            ('qq_upload_media', {'filename': 'x', 'content_base64': 'invalid!'}),
                            ('qq_upload_media', {'filename': 'x', 'content_base64': ''}),
                            ('qq_upload_media', {'filename': 'x', 'content_base64': base64.b64encode(b'x' * (MAX_INLINE_MEDIA + 1)).decode()}),
                        ]
                        for name, args in invalid:
                            result = await session.call_tool(name, args)
                            assert result.isError, (name, result)
                            if name == 'qq_upload_media':
                                assert 'invalid!' not in str(result.content)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            sock.close()
        assert fake.closed
    asyncio.run(scenario())


def test_chunked_mcp_limit(tmp_path, monkeypatch):
    async def scenario():
        app, _ = make_app(tmp_path, monkeypatch)
        # Keep the test small but exercise the same no-Content-Length path.
        for middleware in app.user_middleware:
            if middleware.cls.__name__ == 'RequestLimit':
                middleware.kwargs['limit'] = 32
        async def chunks():
            yield b'x' * 20
            yield b'x' * 20
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url='http://testserver', headers=AUTH) as client:
                response = await client.post('/mcp', content=chunks())
                assert response.status_code == 413
    asyncio.run(scenario())
