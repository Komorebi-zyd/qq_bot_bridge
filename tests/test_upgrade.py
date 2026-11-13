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
    async def upload(api,http,scene,chat,source,file_type,file_name=None):
        calls.append(('upload',source)); assert file_name=='x.png'; return 'token'
    monkeypatch.setattr(sdk,'upload_by_url',upload)
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
    async def upload(api,http,scene,chat,url,file_type,file_name=None):
        assert scene=='group' and chat=='g' and url=='https://cdn.qq.com/a?sig=secret'
        calls.append('upload')
        return 'native-token'
    monkeypatch.setattr(sdk,'upload_by_url',upload)
    class API:
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
    async def upload(api,http,scene,chat,url,file_type,file_name=None):
        assert chat=='u' and file_type==expected_type
        assert url==f'https://cdn.qq.com/example.{extension}'
        seen.append('upload')
        return 'url-media-token'
    monkeypatch.setattr(sdk,'upload_by_url',upload)
    class API:
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


def _url_backend(tmp_path, monkeypatch, upload_impl):
    import bridge.sdk as sdk
    cfg=Settings(data=tmp_path,token=TOKEN,send_interval=0,authorized={'c2c:u'})
    store=Store(cfg); backend=sdk.OfficialBackend(store)
    backend.run=Fake().run; backend.connected=backend.api_ready=True
    async def validate(url,hosts): return url
    monkeypatch.setattr(sdk,'validate_public_media_url',validate)
    monkeypatch.setattr(sdk,'upload_by_url',upload_impl)
    class API:
        async def post_c2c_message(self,chat,msg):
            return {'id':'out'}
    backend.api=API()
    return store, backend


def test_upload_bizcode_reason_is_reported(tmp_path, monkeypatch):
    """A format rejection must surface the platform code and its meaning."""
    from bridge.uploader import UploadError
    async def upload(*a, **k):
        raise UploadError(400, 850019)
    store, backend = _url_backend(tmp_path, monkeypatch, upload)
    app=create_app(store.cfg,backend)
    with TestClient(app,headers=AUTH) as c:
        c.post('/v1/messages/send',json=dict(idempotency_key='fmt',scene='c2c',chat_id='u',
            media_url='https://cdn.qq.com/a.bin',media_kind='file'))
        result=terminal(c,'fmt')
        assert result['status']=='failed' and result['stage']=='upload'
        assert result['error_details']=={'status_code':400,'bizcode':850019}
        assert 'unsupported media format' in result['error']
        # Not a download problem, so no local retry is attempted.
        assert result['error'].count('bridge could not fetch') == 0
    store.db.close()


def test_upload_falls_back_to_local_copy(tmp_path, monkeypatch):
    """850026 means the platform could not fetch the URL; retry with our own copy."""
    import bridge.sdk as sdk
    from bridge.uploader import UploadError
    seen=[]
    async def upload(*a, **k):
        seen.append('url')
        raise UploadError(400, 850026)
    store, backend = _url_backend(tmp_path, monkeypatch, upload)
    async def download(http,url,limit):
        seen.append('download'); return b'media-bytes'
    async def local(api,http,scene,chat,data,file_type,file_name):
        seen.append('local'); assert data==b'media-bytes'; return 'local-token'
    monkeypatch.setattr(sdk,'download_media',download)
    monkeypatch.setattr(sdk,'upload_local_copy',local)
    app=create_app(store.cfg,backend)
    with TestClient(app,headers=AUTH) as c:
        c.post('/v1/messages/send',json=dict(idempotency_key='fallback',scene='c2c',chat_id='u',
            media_url='https://cdn.qq.com/a.png',media_kind='image'))
        assert terminal(c,'fallback')['status']=='sent'
        assert seen==['url','download','local']
    store.db.close()


def test_upload_reports_both_failures(tmp_path, monkeypatch):
    import bridge.sdk as sdk
    from bridge.uploader import UploadError
    async def upload(*a, **k):
        raise UploadError(400, 850026)
    store, backend = _url_backend(tmp_path, monkeypatch, upload)
    async def download(*a, **k):
        raise ValueError('unreachable')
    monkeypatch.setattr(sdk,'download_media',download)
    app=create_app(store.cfg,backend)
    with TestClient(app,headers=AUTH) as c:
        c.post('/v1/messages/send',json=dict(idempotency_key='both',scene='c2c',chat_id='u',
            media_url='https://cdn.qq.com/a.png',media_kind='image'))
        result=terminal(c,'both')
        assert result['status']=='failed' and result['stage']=='upload'
        assert 'could not download' in result['error'] and 'bridge could not fetch' in result['error']
        assert result['error_details']['bizcode']==850026
    store.db.close()


def test_uploader_parses_platform_code(monkeypatch):
    import asyncio as aio
    from bridge import uploader
    class Resp:
        status_code=400
        def json(self): return {'code':850026,'message':'download failed'}
    class Http:
        async def post(self,*a,**k): return Resp()
    class Api:
        async def ensure_token(self): return 'token'
    async def scenario():
        try:
            await uploader.upload_by_url(Api(),Http(),'c2c','u','https://cdn.qq.com/a.png',1,'a.png')
        except uploader.UploadError as exc:
            return exc
        raise AssertionError('expected UploadError')
    exc=aio.run(scenario())
    assert exc.status_code==400 and exc.bizcode==850026 and exc.can_retry_locally


def test_undocumented_upload_code_still_retries_locally():
    from bridge.uploader import UploadError
    exc=UploadError(400, 40093007)
    assert exc.can_retry_locally
    assert '40093007' in exc.reason
    for code in (850019, 850031, 850018):
        assert not UploadError(400, code).can_retry_locally


def test_group_info_cached_once(tmp_path):
    import bridge.sdk as sdk
    cfg=Settings(data=tmp_path,token=TOKEN)
    store=Store(cfg); backend=sdk.OfficialBackend(store)
    calls=[]
    class API:
        async def request(self,route):
            calls.append('info')
            return {'group_openid':'g','group_name':'读书分享会','group_finger_memo':'每周共读',
                    'group_class_text':'文化','group_tags':['阅读'],'group_member_num':256,'secret':'ignore'}
    backend.api=API()
    async def scenario():
        await backend._load_group_info('g')
        await backend._load_group_info('g')
    asyncio.run(scenario())
    assert calls==['info']
    info=store.group_info('g')
    assert info['group_name']=='读书分享会' and info['group_member_num']==256
    assert 'secret' not in info and 'group_openid' not in info
    store.db.close()


def test_group_info_unavailable_is_cached(tmp_path):
    import bridge.sdk as sdk
    cfg=Settings(data=tmp_path,token=TOKEN)
    store=Store(cfg); backend=sdk.OfficialBackend(store)
    calls=[]
    class API:
        async def request(self,route):
            calls.append('info'); raise RuntimeError('QQ Bot API error [403] /v2/groups/x/info: no permission')
    backend.api=API()
    async def scenario():
        await backend._load_group_info('g')
        await backend._load_group_info('g')
    asyncio.run(scenario())
    assert calls==['info'] and store.group_info('g')=={'unavailable':True}
    store.db.close()


def test_sessions_include_group_info(tmp_path):
    cfg=Settings(data=tmp_path,token=TOKEN,send_interval=0)
    app=create_app(cfg,Fake())
    with TestClient(app,headers=AUTH) as c:
        store=app.state.store
        store.observe(dict(message_id='g1',scene='group',chat_id='g',user_id='u',timestamp=iso()))
        store.set_group_info('g',{'group_name':'读书分享会','group_member_num':12})
        item=c.get('/v1/sessions').json()['items'][0]
        assert item['group']['group_name']=='读书分享会'
        store.observe(dict(message_id='c1',scene='c2c',chat_id='u',user_id='u',timestamp=iso()))
        c2c=next(x for x in c.get('/v1/sessions').json()['items'] if x['scene']=='c2c')
        assert 'group' not in c2c
