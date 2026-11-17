"""Opt-in real PostgreSQL coverage for MCP Streamable HTTP, using only a local fake QQ."""
import asyncio
import json
import os
import socket
import time
import uuid
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bridge.core import Settings, iso
from bridge.mcp_server import create_combined_app

TOKEN = 'offline-postgres-mcp-token-0123456789'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def pg_url():
    """A fresh schema in the explicitly supplied dedicated test database."""
    url = os.getenv('BRIDGE_TEST_POSTGRES_URL', '')
    if not url:
        pytest.skip('set BRIDGE_TEST_POSTGRES_URL to a dedicated local PostgreSQL test database')
    import psycopg
    from psycopg import sql
    schema = 'qqbridge_mcp_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(url)
    query = parse_qsl(parts.query) + [('options', '-c search_path=' + schema)]
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


class FakeQQ:
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
        return {'id': 'fake-sent', 'ext_info': {'ref_idx': 'REFIDX_fake_sent'}}


def same(value):
    if isinstance(value, dict):
        return {key: same(item) for key, item in value.items()
                if key != 'reply_remaining_seconds'}
    if isinstance(value, list):
        return [same(item) for item in value]
    return value


async def wait_active(call):
    for _ in range(200):
        status = await call('qq_status')
        if status.get('storage', {}).get('active'):
            return status
        await asyncio.sleep(.025)
    pytest.fail('PostgreSQL MCP application did not become active')


async def wait_sent(call, key):
    for _ in range(200):
        result = await call('qq_get_send_result', {'key': key})
        if result.get('status') == 'sent':
            return result
        await asyncio.sleep(.025)
    pytest.fail('fake QQ send did not finish')


def test_postgres_streamable_mcp_persists_across_app_restart(tmp_path, pg_url, monkeypatch):
    async def scenario():
        async def serve(data_dir, fake):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            host = f'127.0.0.1:{sock.getsockname()[1]}'
            monkeypatch.setenv('BRIDGE_ALLOWED_HOSTS', host)
            monkeypatch.setenv('BRIDGE_ALLOWED_ORIGINS', 'http://agent.example')
            app = create_combined_app(Settings(data=data_dir, token=TOKEN,
                                      database_url=pg_url, send_interval=0), fake)
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            for _ in range(200):
                if server.started:
                    return app, server, task, sock, host
                if task.done():
                    await task
                await asyncio.sleep(.01)
            pytest.fail('uvicorn did not start')

        async def stop(server, task, sock):
            server.should_exit = True
            await asyncio.wait_for(task, 15)
            sock.close()

        first_fake = FakeQQ()
        app, server, task, sock, host = await serve(tmp_path / 'first', first_fake)
        try:
            async with httpx.AsyncClient(headers=AUTH, timeout=15) as http:
                async with streamable_http_client(f'http://{host}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, arguments=None):
                            result = await session.call_tool(name, arguments or {})
                            assert not result.isError, result
                            return json.loads(result.content[0].text)

                        status = await wait_active(call)
                        assert status['storage'] == {'backend': 'postgresql', 'active': True}
                        assert same(status) == same((await http.get(f'http://{host}/v1/status')).json())
                        caps = await call('qq_get_capabilities')
                        assert caps == (await http.get(f'http://{host}/v1/capabilities?detail=lite')).json()

                        seq = await app.state.store.call(app.state.store.observe, dict(
                            message_id='postgres-inbound', scene='c2c', chat_id='user', user_id='user',
                            content='persist me', sender={'username': 'Tester'}, timestamp=iso(),
                            reference_index='REFIDX_postgres_inbound'))
                        messages = await call('qq_get_messages', {'scene': 'c2c', 'chat_id': 'user'})
                        assert messages['items'][0]['seq'] == seq
                        assert (await call('qq_ack_message', {'seq': seq}))['acked']
                        body = {'idempotency_key': 'postgres-mcp-send', 'reply_to': seq,
                                'quote_to': seq, 'text': 'quoted answer'}
                        assert (await call('qq_send_message', body))['key'] == body['idempotency_key']
                        sent = await wait_sent(call, body['idempotency_key'])
                        assert sent['status'] == 'sent'
                        assert first_fake.calls[-1][0]['quote_to'] == seq
                        history = await call('qq_get_session_history', {'scene': 'c2c', 'chat_id': 'user'})
                        assert [item['direction'] for item in history['items']] == ['in', 'out']
        finally:
            await stop(server, task, sock)
        assert first_fake.closed

        # A wholly new app and empty local directory must read the PostgreSQL history.
        second_fake = FakeQQ()
        app2, server2, task2, sock2, host2 = await serve(tmp_path / 'second', second_fake)
        try:
            async with httpx.AsyncClient(headers=AUTH, timeout=15) as http:
                async with streamable_http_client(f'http://{host2}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, arguments=None):
                            result = await session.call_tool(name, arguments or {})
                            assert not result.isError, result
                            return json.loads(result.content[0].text)

                        await wait_active(call)
                        history = await call('qq_get_session_history', {'scene': 'c2c', 'chat_id': 'user'})
                        assert [item['direction'] for item in history['items']] == ['in','out']
                        assert history['items'][0]['seq']==seq
                        assert history['items'][1]['key']==body['idempotency_key']
                        assert history['items'][1]['quote_to']==seq
                        replay = await call('qq_send_message', body)
                        assert replay == await call('qq_get_send_result', {'key': body['idempotency_key']})
                        assert second_fake.calls == []
                        assert not (tmp_path / 'second' / 'bridge.sqlite3').exists()
        finally:
            await stop(server2, task2, sock2)
        assert second_fake.closed
    asyncio.run(scenario())
