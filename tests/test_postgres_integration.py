"""Opt-in real PostgreSQL contract tests, isolated in fresh test schemas.

BRIDGE_TEST_POSTGRES_URL is a dedicated test database, never a production URI.
Each test creates its own schema and retains it for inspection; no data is dropped.
"""
import asyncio
import json
import os
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote

import pytest
from fastapi.testclient import TestClient

from bridge.core import Settings, Store, create_app, iso, resolve_quote
from bridge.ownership import DatabaseOwner

TOKEN='offline-postgres-test-token-0123456789'
AUTH={'Authorization':'Bearer '+TOKEN}


@pytest.fixture
def pg_url():
    url=os.getenv('BRIDGE_TEST_POSTGRES_URL','')
    if not url:
        pytest.skip('set BRIDGE_TEST_POSTGRES_URL to a dedicated PostgreSQL test database')
    import psycopg
    from psycopg import sql
    schema='qqbridge_test_'+uuid.uuid4().hex
    with psycopg.connect(url,autocommit=True) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts=urlsplit(url)
    query=parse_qsl(parts.query)
    query.append(('options','-c search_path='+schema))
    return urlunsplit(parts._replace(query=urlencode(query,quote_via=quote)))


def owned_store(path,url):
    store=Store(Settings(data=path,token=TOKEN,database_url=url,send_interval=0))
    owner=DatabaseOwner(store.db)
    assert owner.try_acquire()
    store.initialize()
    return store,owner


class Fake:
    connected=api_ready=True
    reconnecting=False

    def __init__(self):
        self.calls=[]
        self.runs=0

    async def run(self):
        self.runs+=1
        await asyncio.Event().wait()

    async def close(self):
        pass

    async def send(self,body,route):
        self.calls.append((body,route))
        return {'id':'fake-platform-id','ext_info':{'ref_idx':'REFIDX_out=='+body['idempotency_key']}}


def wait_active(client):
    for _ in range(100):
        status=client.get('/v1/status').json()
        if status.get('storage',{}).get('active'):
            return status
        time.sleep(.05)
    pytest.fail('PostgreSQL owner did not become active')


def wait_sent(client,key):
    for _ in range(100):
        result=client.get('/v1/sends/'+key).json()
        if result.get('status')=='sent':
            return result
        time.sleep(.05)
    pytest.fail('fake PostgreSQL send did not finish')


def test_store_persists_across_new_empty_local_directory(tmp_path,pg_url):
    store,owner=owned_store(tmp_path/'old-container',pg_url)
    try:
        seq=store.observe(dict(message_id='in',scene='c2c',chat_id='u',user_id='u',content='persistent',
            sender={'username':'Alice'},timestamp=iso(),reference_index='REFIDX_in=='))
        store.set_session('app','session-test',90)
        store.set_group_info('g',{'group_name':'Persisted Group','group_member_num':3})
        with store.db:
            store.db.execute('UPDATE messages SET acked=1 WHERE seq=?',(seq,))
            body=dict(idempotency_key='saved-send',scene='c2c',chat_id='u',text='old bot message')
            store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',('saved-send','fp',json.dumps(body),'sent',iso(),iso(),None,
                json.dumps({'ext_info':{'ref_idx':'REFIDX_saved=='}})))
            store.db.execute('INSERT INTO send_routes VALUES(?,?,?,NULL)',('saved-send','c2c','u'))
            store.db.execute('INSERT INTO send_details VALUES(?,?,?,NULL)',('saved-send','sent',None))
        store.index_send('saved-send')
    finally:
        owner.release();store.db.close()
    new,owner=owned_store(tmp_path/'new-container',pg_url)
    try:
        assert not (tmp_path/'new-container'/'bridge.sqlite3').exists()
        assert new.message(seq)['content']=='persistent' and new.message(seq)['acked']
        assert new.profile('c2c','u','u')['username']=='Alice'
        assert new.get_session('app')==('session-test',90)
        assert new.group_info('g')['group_name']=='Persisted Group'
        assert resolve_quote(new,{'quote_to':seq},'c2c','u')=='REFIDX_in=='
        assert resolve_quote(new,{'quote_key':'saved-send'},'c2c','u')=='REFIDX_saved=='
        quote_seq=new.observe(dict(message_id='q',scene='c2c',chat_id='u',user_id='u',timestamp=iso(),
            quote={'reference_index':'REFIDX_saved==','elements':[{'content':'old bot message'}]}))
        assert new.message(quote_seq)['quote']['key']=='saved-send'
        assert len(new.session_history('c2c','u',0,50,'all')['items'])==3
        assert new.observe(dict(message_id='in',scene='c2c',chat_id='u',user_id='u'))==seq
    finally:
        owner.release();new.db.close()


def test_postgres_transaction_rollback_then_reuse(tmp_path,pg_url):
    store,owner=owned_store(tmp_path,pg_url)
    try:
        with pytest.raises(ValueError):
            with store.db:
                store.db.execute("INSERT INTO metadata VALUES('test','rollback','not committed')")
                raise ValueError('rollback this test')
        assert store.db.execute("SELECT value FROM metadata WHERE namespace='test'").fetchone() is None
        store.set_group_info('g',{'group_name':'first'})
        store.set_group_info('g',{'group_name':'updated'})
        assert store.group_info('g')['group_name']=='updated'
        with store.db:
            store.db.execute('INSERT INTO media VALUES(?,?,?)',('f'*32,5,time.time()+900))
        assert store.db.execute('SELECT expires FROM media').fetchone()[0]>time.time()+899
    finally:
        owner.release();store.db.close()


def test_render_overlap_standby_and_handoff(tmp_path,pg_url):
    first_fake,second_fake=Fake(),Fake()
    cfg1=Settings(data=tmp_path/'first',token=TOKEN,database_url=pg_url,send_interval=0)
    cfg2=Settings(data=tmp_path/'second',token=TOKEN,database_url=pg_url,send_interval=0)
    first=create_app(cfg1,first_fake)
    second=create_app(cfg2,second_fake)
    with TestClient(second,headers=AUTH) as replacement:
        wait_active(replacement)
        with TestClient(first,headers=AUTH) as previous:
            assert previous.get('/health').json()=={'ok':True}
            assert previous.get('/v1/status').json()['storage']['active'] is False
            assert previous.get('/v1/messages').status_code==503
            assert first_fake.runs==0
            seq=second.state.store.observe(dict(message_id='turn',scene='c2c',chat_id='u',user_id='u',content='hi',
                timestamp=iso(),reference_index='REFIDX_turn'))
            body=dict(idempotency_key='one-send',reply_to=seq,quote_to=seq,text='fake reply')
            assert replacement.post('/v1/messages/send',json=body).status_code==202
            wait_sent(replacement,'one-send')
            assert len(second_fake.calls)==1 and first_fake.calls==[]
        # first was the standby and closed; second remains owner.
        assert replacement.get('/v1/status').json()['storage']['active'] is True
    third_fake=Fake()
    third=create_app(Settings(data=tmp_path/'third',token=TOKEN,database_url=pg_url,send_interval=0),third_fake)
    with TestClient(third,headers=AUTH) as fresh:
        wait_active(fresh)
        assert fresh.post('/v1/messages/send',json=body).status_code==202
        assert third_fake.calls==[]
        assert fresh.get('/v1/messages').json()['items'][0]['seq']==seq
        assert fresh.get('/v1/sends/one-send').json()['status']=='sent'
        assert fresh.post('/v1/messages/send',json={**body,'text':'different'}).status_code==409


def test_live_standby_takes_over_after_old_container_exits(tmp_path,pg_url):
    from contextlib import ExitStack
    old_fake,new_fake=Fake(),Fake()
    old=create_app(Settings(data=tmp_path/'old',token=TOKEN,database_url=pg_url,send_interval=0),old_fake)
    new=create_app(Settings(data=tmp_path/'new',token=TOKEN,database_url=pg_url,send_interval=0),new_fake)
    old_stack=ExitStack()
    old_client=old_stack.enter_context(TestClient(old,headers=AUTH))
    try:
        wait_active(old_client)
        seq=old.state.store.observe(dict(message_id='persisted',scene='c2c',chat_id='u',user_id='u',content='from old',timestamp=iso()))
        with TestClient(new,headers=AUTH) as new_client:
            assert new_client.get('/v1/status').json()['storage']['active'] is False
            assert new_fake.runs==0
            old_stack.close()
            wait_active(new_client)
            assert new_fake.runs==1
            assert new_client.get('/v1/messages').json()['items'][0]['seq']==seq
            assert new_client.post('/v1/messages/send',json=dict(idempotency_key='after-handoff',reply_to=seq,text='new owner')).status_code==202
            wait_sent(new_client,'after-handoff')
            assert len(new_fake.calls)==1 and old_fake.calls==[]
    finally:
        old_stack.close()


def test_recovery_marks_interrupted_send_unknown_only_after_ownership(tmp_path,pg_url):
    store,owner=owned_store(tmp_path/'seed',pg_url)
    try:
        body=dict(idempotency_key='interrupted',scene='c2c',chat_id='u',text='uncertain')
        with store.db:
            store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',('interrupted','fp',json.dumps(body),'sending',iso(),iso(),None,None))
            store.db.execute('INSERT INTO send_routes VALUES(?,?,?,NULL)',('interrupted','c2c','u'))
            store.db.execute('INSERT INTO send_details VALUES(?,?,?,NULL)',('interrupted','send',None))
        standby=Store(Settings(data=tmp_path/'standby',token=TOKEN,database_url=pg_url))
        standby_owner=DatabaseOwner(standby.db)
        try:
            assert not standby_owner.try_acquire()
            assert store.send_record('interrupted')['status']=='sending'
            owner.release()
            assert standby_owner.try_acquire()
            standby.initialize()
            assert standby.send_record('interrupted')['status']=='unknown'
            assert standby.send_record('interrupted')['stage']=='unknown'
            assert any(x.get('idempotency_key')=='interrupted' for x in standby.session_history('c2c','u',0,50,'all')['items'])
        finally:
            standby_owner.release();standby.db.close()
    finally:
        owner.release();store.db.close()


def test_real_connection_loss_reacquires_and_keeps_data(tmp_path,pg_url):
    import psycopg
    fake=Fake()
    app=create_app(Settings(data=tmp_path,token=TOKEN,database_url=pg_url,send_interval=0),fake)
    with TestClient(app,headers=AUTH) as client:
        wait_active(client)
        seq=app.state.store.observe(dict(message_id='before-disconnect',scene='c2c',chat_id='u',user_id='u',content='retained',timestamp=iso()))
        old_generation=app.state.store.db.generation
        pid=app.state.store.db.execute('SELECT pg_backend_pid()').fetchone()[0]
        # Terminate only this test's own backend, never a user database session.
        with psycopg.connect(pg_url,autocommit=True) as admin:
            assert admin.execute('SELECT pg_terminate_backend(%s)',(pid,)).fetchone()[0]
        status=client.get('/v1/status')
        assert status.status_code==200 and status.json()['storage']['active'] is False
        wait_active(client)
        assert app.state.store.db.generation>old_generation
        assert client.get('/v1/messages').json()['items'][0]['seq']==seq
        assert fake.calls==[]


def test_enqueue_suspended_during_ownership_change_does_not_write(tmp_path,pg_url,monkeypatch):
    import bridge.core as core
    fake=Fake()
    app=create_app(Settings(data=tmp_path,token=TOKEN,database_url=pg_url,send_interval=0),fake)
    async def change_lease(url,hosts):
        # Model a request awaiting media validation while its lease is replaced.
        await app.state.store.call(app.state.database_owner.release)
        return url
    monkeypatch.setattr(core,'validate_public_media_url',change_lease)
    with TestClient(app,headers=AUTH) as client:
        wait_active(client)
        reply=client.post('/v1/messages/send',json=dict(idempotency_key='stale-request',scene='c2c',chat_id='u',
            media_url='https://cdn.qq.com/test.png',media_kind='image'))
        assert reply.status_code==503
        import psycopg
        with psycopg.connect(pg_url,autocommit=True) as inspect:
            assert inspect.execute('SELECT key FROM sends WHERE key=%s',('stale-request',)).fetchone() is None
        assert fake.calls==[]


def test_terminal_send_missing_history_is_reconciled_on_restart(tmp_path,pg_url):
    store,owner=owned_store(tmp_path/'old-terminal',pg_url)
    try:
        body=dict(idempotency_key='unindexed',scene='c2c',chat_id='u',text='stored before index')
        with store.db:
            store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',('unindexed','fp',json.dumps(body),'sent',iso(),iso(),None,
                json.dumps({'ext_info':{'ref_idx':'REFIDX_unindexed'}})))
            store.db.execute('INSERT INTO send_routes VALUES(?,?,?,NULL)',('unindexed','c2c','u'))
            store.db.execute('INSERT INTO send_details VALUES(?,?,?,NULL)',('unindexed','sent',None))
    finally:
        owner.release();store.db.close()
    new,owner=owned_store(tmp_path/'new-terminal',pg_url)
    try:
        history=new.session_history('c2c','u',0,50,'all')['items']
        assert len(history)==1 and history[0]['idempotency_key']=='unindexed'
        row=new.db.execute('SELECT send_key FROM reference_lookup WHERE reference_index=?',('REFIDX_unindexed',)).fetchone()
        assert row and row[0]=='unindexed'
    finally:
        owner.release();new.db.close()


def test_inbound_missing_history_is_reconciled_on_restart(tmp_path,pg_url):
    store,owner=owned_store(tmp_path/'old-inbound',pg_url)
    try:
        payload=dict(message_id='unindexed-in',scene='c2c',chat_id='u',user_id='u',content='saved',
            timestamp=iso(),sender={'username':'Persisted'},reference_index='REFIDX_unindexed_in')
        with store.db:
            store.db.execute('INSERT INTO messages(message_id,scene,chat_id,payload) VALUES(?,?,?,?)',
                ('unindexed-in','c2c','u',json.dumps(payload)))
    finally:
        owner.release();store.db.close()
    new,owner=owned_store(tmp_path/'new-inbound',pg_url)
    try:
        assert len(new.session_history('c2c','u',0,50,'all')['items'])==1
        assert new.profile('c2c','u','u')['username']=='Persisted'
        row=new.db.execute('SELECT inbound_seq FROM reference_lookup WHERE reference_index=?',('REFIDX_unindexed_in',)).fetchone()
        assert row and row[0]==new.messages(0,50)['items'][0]['seq']
    finally:
        owner.release();new.db.close()
