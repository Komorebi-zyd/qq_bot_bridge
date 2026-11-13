"""Upgrade contract tests: offline only, including legacy schemas and SDK fakes."""
import asyncio
import json
import socket
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from bridge.core import Settings, Store, create_app, iso, epoch
from bridge.media_url import validate_media_url, validate_public_media_url
from test_bridge import Fake, TOKEN, AUTH, terminal, offline_only


@pytest.mark.parametrize('url',[
    'http://example.com/a','ftp://example.com/a','https://user:secret@example.com/a',
    'https://example.com/a\n','not-a-url','','https://'])
def test_url_syntax(url):
    with pytest.raises(ValueError) as error:
        validate_media_url(url)
    if url:
        assert url not in str(error.value)


@pytest.mark.parametrize('url',[
    'https://127.0.0.1/a','https://localhost/a','https://example.com:8443/a',
    'https://169.254.169.254/a','https://[::1]/a'])
def test_url_accepted_as_platform_requires(url):
    # The QQ platform fetches the media, so the bridge does not add its own
    # address filtering on top of the platform's rules.
    assert validate_media_url(url)


def test_signed_cdn_url_and_allowlist():
    url='https://multimedia.nt.qq.com.cn/download?appid=1&fileid=a&rkey=secret%2Bkey'
    assert validate_media_url(url)=='multimedia.nt.qq.com.cn'
    assert validate_media_url(url,{'multimedia.nt.qq.com.cn'})
    with pytest.raises(ValueError): validate_media_url(url,{'qq.com'})


def test_config_defaults_and_empty(monkeypatch):
    monkeypatch.setenv('BRIDGE_REPLY_WINDOW_C2C','  ')
    monkeypatch.setenv('BRIDGE_MAX_REPLIES_PER_MESSAGE','')
    monkeypatch.setenv('BRIDGE_SEND_INTERVAL_SECONDS',' ')
    monkeypatch.setenv('BRIDGE_PROACTIVE_SESSIONS',' c2c:known, ,group:g ')
    cfg=Settings()
    assert cfg.windows==dict(c2c=3600,group=300,guild=300,dm=300)
    assert cfg.reply_limits==dict(c2c=4,group=5,guild=5,dm=5)
    assert cfg.authorized=={'c2c:known','group:g'}
    assert Settings(max_replies=1).reply_limits==dict.fromkeys(cfg.windows,1)
    assert Settings(max_replies=100).reply_limits==cfg.reply_limits
    monkeypatch.setenv('BRIDGE_MAX_FILE_BYTES','sensitive-invalid')
    with pytest.raises(RuntimeError) as exc: Settings()
    assert 'BRIDGE_MAX_FILE_BYTES' in str(exc.value) and 'sensitive-invalid' not in str(exc.value)


def test_profiles_history_limits(tmp_path):
    cfg=Settings(data=tmp_path,token=TOKEN,send_interval=0)
    app=create_app(cfg,Fake())
    with TestClient(app,headers=AUTH) as c:
        store=app.state.store
        first=store.observe(dict(message_id='1',scene='c2c',chat_id='u',user_id='u',timestamp=iso(),sender={'username':'first','avatar':'https://cdn.qq.com/a','token':'secret'}))
        store.observe(dict(message_id='2',scene='c2c',chat_id='u',user_id='u',timestamp=iso(),sender={'username':'latest'}))
        msg=c.get('/v1/messages').json()['items'][0]
        assert msg['sender']['username']=='first' and msg['sender_profile']['username']=='latest'
        assert msg['sender_profile']['qq_number'] is None and 'token' not in str(msg)
        assert msg['sender']['name_source']=='official_event'
        assert msg['proactive_allowed'] and msg['replies_remaining']==4
        assert c.get('/v1/profiles?scene=c2c&chat_id=u').json()['items'][0]['username']=='latest'
        assert c.get('/v1/profiles/c2c/u/u').json()['qq_number'] is None
        assert c.get('/v1/profiles/c2c/u/missing').status_code==404
        for n in range(4):
            assert c.post('/v1/messages/send',json=dict(idempotency_key=str(n),reply_to=first,text='hello')).status_code==202
            assert terminal(c,str(n))['status']=='sent'
            assert c.get('/v1/sends/'+str(n)).json()['msg_seq']==n+1
        assert c.post('/v1/messages/send',json=dict(idempotency_key='fifth',reply_to=first,text='x')).status_code==409
        msg=c.get('/v1/messages').json()['items'][0]
        assert not msg['can_reply'] and msg['replies_remaining']==0
        inbound=c.get('/v1/messages').json()
        assert len(inbound['items'])==2
        history=c.get('/v1/sessions/c2c/u/history').json()
        assert [x['direction'] for x in history['items']]==['in','in','out','out','out','out']
        assert all(x['delivered'] and 'seq' not in x for x in history['items'][2:])
        assert len(c.get('/v1/sessions/c2c/u/history?direction=out').json()['items'])==4
        assert c.get('/v1/capabilities').json()['scenes']['c2c']['max_replies']==4
        assert c.get('/v1/status').json()['proactive'] is True


def test_legacy_database_idempotent_migration(tmp_path):
    db=sqlite3.connect(tmp_path/'bridge.sqlite3')
    db.executescript('CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT,scene TEXT,chat_id TEXT,payload TEXT,acked INTEGER DEFAULT 0,UNIQUE(scene,chat_id,message_id)); CREATE TABLE sends(key TEXT PRIMARY KEY,fingerprint TEXT,payload TEXT,status TEXT,created_at TEXT,updated_at TEXT,error TEXT,result TEXT);')
    timestamp=iso(time.time()-1000)
    payload=dict(message_id='legacy',scene='c2c',chat_id='u',user_id='u',timestamp=timestamp,received_at=iso(),reply_deadline=iso(time.time()-1))
    db.execute('INSERT INTO messages VALUES(42,?,?,?,?,1)',('legacy','c2c','u',json.dumps(payload)))
    body=dict(idempotency_key='old-send',reply_to=42,text='old',media_id=None,media_kind=None,markdown=False,scene=None,chat_id=None)
    db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',('old-send','old-fingerprint',json.dumps(body),'sent',iso(),iso(),None,'{"id":"old-out"}'))
    db.commit(); db.close()
    cfg=Settings(data=tmp_path,token=TOKEN)
    for _ in range(2):
        store=Store(cfg)
        assert store.message(42)['acked']
        assert epoch(store.message(42)['reply_deadline'])==epoch(timestamp)+3600
        assert store.reply_count(42)==1
        assert len(store.db.execute('PRAGMA table_info(sends)').fetchall())==8
        assert len(store.session_history('c2c','u',0,50,'all')['items'])==2
        store.db.close()
    app=create_app(cfg,Fake())
    with TestClient(app,headers=AUTH) as c:
        assert c.post('/v1/messages/send',json=body).status_code==202
        assert c.get('/v1/messages?after=41').json()['items'][0]['seq']==42


def test_url_enqueue_then_sdk_recheck_native_upload(tmp_path,monkeypatch):
    import bridge.core as core
    import bridge.sdk as sdk
    from qqbot_agent_sdk.media_loader import MediaUploader
    calls=[]
    async def validate(url,allowed):
        calls.append(('validate',url)); return url
    monkeypatch.setattr(core,'validate_public_media_url',validate)
    monkeypatch.setattr(sdk,'validate_public_media_url',validate)
    async def upload(self,scene,chat,source,kind,file_name=None):
        calls.append(('upload',source)); assert file_name=='x.png'; return 'token'
    monkeypatch.setattr(MediaUploader,'upload',upload)
    cfg=Settings(data=tmp_path,token=TOKEN,send_interval=0,authorized={'c2c:u'})
    store=Store(cfg); backend=sdk.OfficialBackend(store)
    backend.run=Fake().run; backend.connected=backend.api_ready=True
    class API:
        async def post_c2c_message(self,chat,msg):
            calls.append(('send',msg.msg_seq)); return {'id':'out'}
    backend.api=API()
    app=create_app(cfg,backend)
    with TestClient(app,headers=AUTH) as c:
        body=dict(idempotency_key='url',scene='c2c',chat_id='u',media_url='https://cdn.qq.com/a?rkey=secret',media_kind='image',filename='x.png')
        assert c.post('/v1/messages/send',json=body).status_code==202
        assert terminal(c,'url')['status']=='sent'
        assert [x[0] for x in calls]==['validate','validate','upload','send']
        history=c.get('/v1/sessions/c2c/u/history').json()
        assert 'rkey' not in str(history)
        bad=c.post('/v1/messages/send',json={**body,'idempotency_key':'bad','media_url':'https://user:secret@cdn.qq.com/a'})
        assert bad.status_code==422 and 'secret' not in bad.text
    store.db.close()


def test_upload_failure_is_definite_and_safe(tmp_path,monkeypatch):
    from bridge.sdk import OfficialBackend
    from qqbot_agent_sdk.media_loader import MediaUploader
    cfg=Settings(data=tmp_path,token=TOKEN,send_interval=0,authorized={'c2c:u'})
    store=Store(cfg); backend=OfficialBackend(store)
    backend.run=Fake().run; backend.connected=backend.api_ready=True
    async def upload(*args,**kw): raise RuntimeError('QQ Bot API error [500] code=123 secret signed-url')
    monkeypatch.setattr(MediaUploader,'upload',upload)
    app=create_app(cfg,backend)
    with TestClient(app,headers=AUTH) as c:
        mid=c.post('/v1/media',files={'file':('x.png',b'fake')}).json()['media_id']
        c.post('/v1/messages/send',json=dict(idempotency_key='upload',scene='c2c',chat_id='u',media_id=mid,media_kind='image'))
        result=terminal(c,'upload')
        assert result['status']=='failed' and result['stage']=='upload'
        assert result['error_details']=={'status_code':500,'bizcode':123}
        assert 'secret' not in str(result)
        assert not c.get('/v1/sessions/c2c/u/history').json()['items'][0]['delivered']
    store.db.close()


def test_expired_queue_offline(tmp_path,monkeypatch):
    import bridge.core as core
    clock=[time.time()]
    monkeypatch.setattr(core.time,'time',lambda:clock[0])
    fake=Fake(); fake.connected=fake.api_ready=False
    app=create_app(Settings(data=tmp_path,token=TOKEN),fake)
    with TestClient(app,headers=AUTH) as c:
        seq=app.state.store.observe(dict(message_id='x',scene='group',chat_id='g',timestamp=iso(clock[0])))
        c.post('/v1/messages/send',json=dict(idempotency_key='expire',reply_to=seq,text='x'))
        clock[0]+=301
        assert terminal(c,'expire')['status']=='failed'
        assert not fake.calls


def test_real_sdk_native_url_upload_and_inbound_identity(tmp_path,monkeypatch):
    import bridge.sdk as sdk
    cfg=Settings(data=tmp_path,token=TOKEN,authorized={'group:g'})
    store=Store(cfg); backend=sdk.OfficialBackend(store)
    async def validate(url,hosts): return url
    monkeypatch.setattr(sdk,'validate_public_media_url',validate)
    calls=[]
    class API:
        async def upload_group_file(self,chat,msg):
            body=msg.to_dict()
            assert chat=='g' and body['url']=='https://cdn.qq.com/a?sig=secret'
            assert body['srv_send_msg'] is False
            calls.append('upload')
            return {'file_info':'native-token'}
        async def post_group_message(self,chat,msg):
            assert msg.media.file_info=='native-token'
            calls.append('send')
            return {'id':'out'}
    backend.api=API()
    body=dict(media_url='https://cdn.qq.com/a?sig=secret',media_kind='image',text='x',markdown=False,scene='group',chat_id='g')
    assert asyncio.run(backend.send(body,('group','g',None)))['id']=='out'
    assert calls==['upload','send']
    raw=dict(id='incoming',timestamp=iso(),group_openid='g',author=dict(member_openid='opaque',username='Name',member_role='admin',bot=False,avatar='https://cdn.qq.com/avatar',union_openid='not-qq'),content='hi',mentions=[dict(id='other',username='Other',secret='ignore')],attachments=[dict(url='https://cdn.qq.com/image',width=640,height=480,asr_refer_text='platform')])
    asyncio.run(backend.on_message('GROUP_AT_MESSAGE_CREATE',raw))
    msg=store.messages(0,10)['items'][0]
    assert msg['sender']['username']=='Name' and msg['sender']['member_role']=='admin'
    assert msg['sender_profile']['qq_number'] is None
    assert msg['mentions'][0]['user_id']=='other' and msg['mentions'][0]['source']=='raw_event'
    assert 'secret' not in str(msg['mentions'])
    assert msg['attachments'][0]['width']==640 and msg['attachments'][0]['height']==480
    assert msg['asr_text']=='platform'
    assert msg['replies_remaining']==5
    assert msg['proactive_allowed']
    store.db.close()


@pytest.mark.parametrize('kind,expected_type,extension', [('image',1,'png'),('video',2,'mp4'),('voice',3,'wav'),('file',4,'pdf')])
def test_url_all_media_kinds_and_reserved_sequence(tmp_path,monkeypatch,kind,expected_type,extension):
    import bridge.sdk as sdk
    store=Store(Settings(data=tmp_path,token=TOKEN))
    backend=sdk.OfficialBackend(store)
    async def validate(url,hosts): return url
    monkeypatch.setattr(sdk,'validate_public_media_url',validate)
    seen=[]
    class API:
        async def upload_c2c_file(self,chat,msg):
            data=msg.to_dict()
            assert chat=='u' and data['file_type']==expected_type
            assert data['url']==f'https://cdn.qq.com/example.{extension}'
            assert data['srv_send_msg'] is False
            seen.append('upload')
            return {'file_info':'url-media-token'}
        async def post_c2c_message(self,chat,msg):
            assert msg.msg_seq==3 and msg.msg_type==7 and msg.msg_id=='original-id'
            assert msg.media.file_info=='url-media-token'
            seen.append('send')
            return {'id':'out'}
    backend.api=API()
    seq=store.observe(dict(message_id='original-id',scene='c2c',chat_id='u',user_id='u',timestamp=iso()))
    body=dict(media_url=f'https://cdn.qq.com/example.{extension}',media_kind=kind,text='caption',markdown=False,
              reply_to=seq,_msg_seq=3)
    assert asyncio.run(backend.send(body,('c2c','u','original-id')))['id']=='out'
    assert seen==['upload','send']
    store.db.close()


def test_profile_keeps_name_when_next_event_lacks_it(tmp_path):
    store=Store(Settings(data=tmp_path,token=TOKEN))
    seq=store.observe(dict(message_id='a',scene='group',chat_id='g',user_id='u',timestamp=iso(),sender={'username':'Alice'}))
    store.observe(dict(message_id='b',scene='group',chat_id='g',user_id='u',timestamp=iso(),sender={'member_role':'admin'}))
    assert store.profile('group','g','u')['username']=='Alice'
    assert store.message(seq)['sender']['username']=='Alice'
    assert store.profile('group','g','u')['member_role']=='admin'
    store.db.close()


def test_legacy_history_interleaves_and_restarts(tmp_path):
    cfg=Settings(data=tmp_path,token=TOKEN)
    store=Store(cfg)
    start=time.time()-100
    one=dict(message_id='first',scene='c2c',chat_id='u',user_id='u',timestamp=iso(start),received_at=iso(start))
    two=dict(message_id='second',scene='c2c',chat_id='u',user_id='u',timestamp=iso(start+20),received_at=iso(start+20))
    with store.db:
        store.db.execute('INSERT INTO messages(message_id,scene,chat_id,payload) VALUES(?,?,?,?)',('first','c2c','u',json.dumps(one)))
        store.db.execute('INSERT INTO messages(message_id,scene,chat_id,payload) VALUES(?,?,?,?)',('second','c2c','u',json.dumps(two)))
        payload=dict(idempotency_key='old',reply_to=1,text='middle')
        store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',('old','fp',json.dumps(payload),'sent',iso(start+10),iso(start+10),None,'{}'))
    store.db.close()
    for _ in range(2):
        store=Store(cfg)
        history=store.session_history('c2c','u',0,50,'all')['items']
        assert [x['direction'] for x in history]==['in','out','in']
        assert history[1]['timestamp']==iso(start+10)
        store.db.close()
