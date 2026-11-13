import asyncio
import inspect
import time
from dataclasses import replace
from importlib.metadata import version
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from bridge.core import Settings, Store, create_app, iso

TOKEN = 'test-token-only-0123456789'
AUTH = {'Authorization':'Bearer '+TOKEN}


class Fake:
    connected = api_ready = True
    reconnecting = False
    def __init__(self, uncertain=False):
        self.calls, self.uncertain = [], uncertain
    async def run(self):
        await asyncio.Event().wait()
    async def close(self):
        pass
    async def send(self, body, route):
        self.calls.append((body,route))
        if self.uncertain:
            raise TimeoutError()
        return {'id':'fake-outbound'}


@pytest.fixture
def setup(tmp_path):
    cfg = Settings(data=tmp_path,token=TOKEN,send_interval=0,authorized={'c2c:allowed'},max_replies=1)
    fake = Fake()
    app = create_app(cfg,fake)
    with TestClient(app,headers=AUTH) as client:
        yield cfg,fake,app,client


def observe(app, message_id='m1', timestamp=None, **kw):
    return app.state.store.observe(dict(message_id=message_id,scene='c2c',chat_id='user',user_id='user',
        content='hello',attachments=[],timestamp=timestamp or iso(),**kw))


def terminal(client,key):
    for _ in range(150):
        record = client.get('/v1/sends/'+key).json()
        if record['status'] in ('sent','failed','unknown'):
            return record
        time.sleep(.01)
    pytest.fail('worker did not finish')


def test_auth_health(setup):
    _,fake,app,c = setup
    fake.connected = fake.api_ready = False
    assert c.get('/health',headers={'Authorization':''}).status_code==200
    for method,path in [('get','/v1/status'),('get','/v1/messages'),('get','/v1/sessions'),
        ('get','/v1/sends/x'),('post','/v1/messages/1/ack'),('post','/v1/messages/send'),('post','/v1/media')]:
        assert getattr(c,method)(path,headers={'Authorization':'Bearer wrong'}).status_code==401
    assert not c.get('/v1/status').json()['connected']


def test_cursors_ack_history_no_loss(setup):
    cfg,fake,app,c = setup
    seqs = [observe(app,str(i)) for i in range(5)]
    assert observe(app,'0')==seqs[0]
    first = c.get('/v1/messages?limit=2').json()
    assert [r['seq'] for r in first['items']]==seqs[:2]
    assert c.post(f'/v1/messages/{seqs[0]}/ack').status_code==200
    rest = c.get('/v1/messages',params={'after':first['next_cursor']}).json()
    assert [r['seq'] for r in rest['items']]==seqs[2:]
    assert c.get('/v1/messages').json()['items'][0]['acked']
    assert c.get('/v1/sessions').json()['items'][0]['message_count']==5
    assert len(c.get('/v1/sessions/c2c/user/messages').json()['items'])==5
    assert c.get('/v1/messages?scene=group&after=3').json()=={'items':[],'next_cursor':3}
    assert c.get('/v1/messages?limit=201').status_code==422


def test_reply_idempotency_routing(setup):
    _,fake,app,c = setup
    seq = observe(app)
    body = {'idempotency_key':'turn-1','reply_to':seq,'text':'world'}
    assert c.post('/v1/messages/send',json=body).status_code==202
    assert terminal(c,'turn-1')['status']=='sent'
    assert c.post('/v1/messages/send',json=body).status_code==202
    assert len(fake.calls)==1
    assert fake.calls[0][1]==('c2c','user','m1')
    assert c.post('/v1/messages/send',json={**body,'text':'changed'}).status_code==409
    assert c.post('/v1/messages/send',json={**body,'idempotency_key':'turn-2'}).status_code==409
    assert c.post('/v1/messages/send',json={**body,'idempotency_key':'wrong','chat_id':'other'}).status_code==409


def test_unknown_never_resent(setup):
    _,fake,app,c = setup
    fake.uncertain = True
    seq=observe(app)
    body={'idempotency_key':'uncertain','reply_to':seq,'text':'test'}
    c.post('/v1/messages/send',json=body)
    assert terminal(c,'uncertain')['status']=='unknown'
    c.post('/v1/messages/send',json=body)
    time.sleep(.1)
    assert len(fake.calls)==1


def test_expiry_authorization_and_validation(setup):
    _,fake,app,c=setup
    seq=observe(app,timestamp=iso(time.time()-4000))
    assert c.post('/v1/messages/send',json={'idempotency_key':'old','reply_to':seq,'text':'x'}).status_code==409
    seq2=observe(app,'bad',timestamp='not-a-time')
    assert c.post('/v1/messages/send',json={'idempotency_key':'bad','reply_to':seq2,'text':'x'}).status_code==409
    assert c.post('/v1/messages/send',json={'idempotency_key':'p','scene':'c2c','chat_id':'denied','text':'x'}).status_code==403
    assert c.post('/v1/messages/send',json={'idempotency_key':'a','scene':'c2c','chat_id':'allowed','text':'x'}).status_code==202
    assert terminal(c,'a')['status']=='sent'
    assert c.post('/v1/messages/send',json={'idempotency_key':'path','text':'x','path':'C:/secret'}).status_code==422
    assert c.post('/v1/messages/send',json={'idempotency_key':'multi','text':'x','media':[{},{}]}).status_code==422


def test_media_isolation_send_and_ttl(setup):
    _,fake,app,c=setup
    result=c.post('/v1/media',files={'file':('../../escape.txt',b'abc','image/png')})
    assert result.status_code==201
    mid=result.json()['media_id']
    assert (app.state.store.media_dir/mid).read_bytes()==b'abc'
    assert not (app.state.store.cfg.data/'escape.txt').exists()
    seq=observe(app)
    body={'idempotency_key':'media','reply_to':seq,'text':'caption','media_id':mid,'media_kind':'image'}
    assert c.post('/v1/messages/send',json=body).status_code==202
    assert terminal(c,'media')['status']=='sent'
    assert len(fake.calls)==1 and fake.calls[0][0]['text']=='caption'
    assert c.post('/v1/messages/send',json={**body,'idempotency_key':'evil','media_id':'../../bad'}).status_code==422
    store=app.state.store
    with store.db:
        store.db.execute('UPDATE media SET expires=0 WHERE id=?',(mid,))
    with pytest.raises(Exception):
        store.media_path(mid)
    store.cleanup()
    assert not (store.media_dir/mid).exists()


def test_global_and_file_limits(tmp_path):
    cfg=Settings(data=tmp_path,token=TOKEN,max_request=512,max_file=16)
    app=create_app(cfg,Fake())
    with TestClient(app,headers=AUTH) as c:
        assert c.post('/v1/media',files={'file':('x',b'x'*17)}).status_code==413
        assert list(app.state.store.media_dir.iterdir())==[]
        assert c.post('/v1/media',files={'file':('x',b'')}).status_code==422
        assert c.post('/v1/messages/send',content=b'x'*513).status_code==413
        assert c.post('/v1/status',content=iter([b'x'*256,b'y'*257])).status_code==413
        assert c.get('/v1/status',headers={**AUTH,'Content-Length':'9999'}).status_code==413


def test_restart_durability_and_sending_unknown(tmp_path):
    cfg=Settings(data=tmp_path,token=TOKEN)
    s=Store(cfg)
    seq=s.observe(dict(message_id='x',scene='group',chat_id='g',user_id='u',timestamp=iso()))
    with s.db:
        s.db.execute('UPDATE messages SET acked=1')
        s.db.execute("INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)",('key','fp','{}','sending',iso(),iso(),None,None))
    s.db.close()
    s=Store(cfg)
    assert s.message(seq)['acked']
    assert s.send_record('key')['status']=='unknown'
    assert s.db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    s.db.close()


def test_sdk_import_signature_and_real_dto(tmp_path):
    from qqbot_agent_sdk import WSCallbacks, QQApiClient, QQWebSocket
    from qqbot_agent_sdk.dto import MessageToCreate, MediaInfo
    from qqbot_agent_sdk.media_loader import MediaUploader
    from bridge.sdk import OfficialBackend
    assert version('qqbot-agent-sdk')=='1.2.2'
    assert inspect.iscoroutinefunction(QQWebSocket.stop)
    assert {'chat_type','chat_id','source','file_type'} <= set(inspect.signature(MediaUploader.upload).parameters)
    assert 'set_heartbeat_interval' in inspect.signature(WSCallbacks).parameters
    assert 'retries' in inspect.signature(QQApiClient.send_text).parameters
    assert MessageToCreate(content='x',msg_type=7,media=MediaInfo(file_info='token')).to_dict()['media']['file_info']=='token'
    store=Store(Settings(data=tmp_path,token=TOKEN))
    backend=OfficialBackend(store)
    class API:
        async def post_c2c_message(self,chat_id,msg):
            assert chat_id=='u' and isinstance(msg,MessageToCreate)
            assert msg.msg_id=='original'
            return {'id':'mock'}
    backend.api=API()
    result=asyncio.run(backend.send({'text':'hi','markdown':False,'media_id':None},('c2c','u','original')))
    assert result['id']=='mock'
    asyncio.run(backend.on_message('C2C_MESSAGE_CREATE',{'id':'incoming','timestamp':iso(),'author':{'user_openid':'u'},
        'content':'','attachments':[{'url':'https://example.invalid/voice','voice_wav_url':'https://example.invalid/wav',
        'content_type':'audio/ogg','asr_refer_text':'平台转写'}]}))
    message=store.messages(0,10)['items'][0]
    assert message['attachments'][0]['voice_wav_url'].endswith('/wav')
    assert message['asr_text']=='平台转写'
    store.db.close()


def test_actual_sdk_media_upload_signature_and_single_send(tmp_path, monkeypatch):
    from bridge.sdk import OfficialBackend
    from qqbot_agent_sdk.media_loader import MediaUploader
    from qqbot_agent_sdk.dto import MessageToCreate
    cfg=Settings(data=tmp_path,token=TOKEN,authorized={'c2c:u'})
    store=Store(cfg)
    mid='a'*32
    (store.media_dir/mid).write_bytes(b'fake-image')
    with store.db:
        store.db.execute('INSERT INTO media VALUES(?,?,?)',(mid,10,time.time()+60))
    uploaded=[]
    async def upload(self,chat_type,chat_id,source,file_type,file_name=None):
        assert source==str((store.media_dir/mid).resolve())
        assert chat_type=='c2c' and chat_id=='u'
        uploaded.append(source)
        return 'file-token'
    monkeypatch.setattr(MediaUploader,'upload',upload)
    backend=OfficialBackend(store)
    sent=[]
    class API:
        async def post_c2c_message(self,chat_id,msg):
            assert isinstance(msg,MessageToCreate)
            assert msg.msg_type==7 and msg.content=='caption'
            assert msg.media.file_info=='file-token'
            sent.append(msg)
            return {'id':'media-out'}
    backend.api=API()
    result=asyncio.run(backend.send({'media_id':mid,'media_kind':'image','text':'caption',
        'markdown':False,'scene':'c2c','chat_id':'u'},('c2c','u',None)))
    assert result['id']=='media-out' and len(uploaded)==len(sent)==1
    store.db.close()


def test_queued_survives_restart(tmp_path):
    cfg=Settings(data=tmp_path,token=TOKEN,authorized={'c2c:allowed'},send_interval=0)
    first=Fake()
    first.connected=first.api_ready=False
    app=create_app(cfg,first)
    with TestClient(app,headers=AUTH) as c:
        c.post('/v1/messages/send',json={'idempotency_key':'survive','scene':'c2c','chat_id':'allowed','text':'queued'})
        assert c.get('/v1/sends/survive').json()['status']=='queued'
    second=Fake()
    app=create_app(cfg,second)
    with TestClient(app,headers=AUTH) as c:
        assert terminal(c,'survive')['status']=='sent'
        assert len(second.calls)==1


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    import socket
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    def local(address):
        # Windows asyncio creates a loopback socket pair for its own wakeup pipe.
        return isinstance(address, tuple) and address[0] in ('127.0.0.1','::1')
    def connect(self, address):
        if local(address): return original(self, address)
        raise AssertionError('external network forbidden in offline tests')
    def connect_ex(self, address):
        if local(address): return original_ex(self, address)
        raise AssertionError('external network forbidden in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)


def test_unauthorized_never_reads_body(monkeypatch):
    import tempfile
    from bridge.core import RequestLimit
    def forbidden(*args, **kwargs):
        pytest.fail('unauthorized body buffered')
    monkeypatch.setattr(tempfile,'SpooledTemporaryFile',forbidden)
    async def app(*args): pytest.fail('unauthorized reached app')
    async def receive(): pytest.fail('unauthorized body read')
    messages=[]
    async def send(msg): messages.append(msg)
    for path in ('/v1/media','/v1/messages/send','/v1'):
        scope={'type':'http','path':path,'headers':[(b'content-length',b'22020096')]}
        asyncio.run(RequestLimit(app,21*1024*1024,TOKEN)(scope,receive,send))
        assert messages[-2]['status']==401


def test_future_deadlines(tmp_path,monkeypatch):
    import bridge.core as core
    monkeypatch.setattr(core.time,'time',lambda:1800000000.0)
    store=Store(Settings(data=tmp_path,token=TOKEN))
    for offset in (-50,0,30,30.001,100000):
        seq=store.observe(dict(message_id=str(offset),scene='c2c',chat_id='u',timestamp=iso(1800000000+offset)))
        deadline=core.epoch(store.message(seq)['reply_deadline'])
        assert deadline==(1800000000+3600+min(offset,0) if offset<=30 else None)
    store.db.close()


def test_session_restart_namespace(tmp_path,monkeypatch):
    from bridge.sdk import OfficialBackend
    cfg=Settings(data=tmp_path,token=TOKEN)
    monkeypatch.setenv('QQBOT_APP_ID','offline-a')
    store=Store(cfg)
    OfficialBackend(store).session_set('fake-session',123)
    store.db.close()
    store=Store(cfg)
    backend=OfficialBackend(store)
    assert backend.session==('fake-session',123)
    monkeypatch.setenv('QQBOT_APP_ID','offline-b')
    other=OfficialBackend(store)
    assert other.session==(None,None)
    other.session_set('other',10)
    backend.session_set(None,None)
    assert store.get_session('offline-a')==(None,None)
    assert store.get_session('offline-b')==('other',10)
    store.db.close()


def test_quote_serialization_and_bot_prefilter(tmp_path,monkeypatch):
    from bridge.sdk import OfficialBackend, EventParser
    store=Store(Settings(data=tmp_path,token=TOKEN))
    backend=OfficialBackend(store)
    raw={'id':'quote','timestamp':iso(),'author':{'user_openid':'u'},'message_type':103,
         'content':'reply','message_reference':{'message_id':'ref'},
         'msg_elements':[{'msg_idx':'ref','content':'nested','attachments':[{'filename':'voice.ogg','url':'//example.invalid/a'}]}]}
    asyncio.run(backend.on_message('C2C_MESSAGE_CREATE',raw))
    msg=store.messages(0,10)['items'][0]
    assert msg['msg_elements'][0]['attachments'][0]['filename']=='voice.ogg'
    assert msg['message_reference']=={'message_id':'ref'}
    def forbidden(*args): pytest.fail('bot payload parsed')
    monkeypatch.setattr(EventParser,'parse',forbidden)
    asyncio.run(backend.on_message('C2C_MESSAGE_CREATE',{**raw,'id':'bot','author':{'bot':True}}))
    assert len(store.messages(0,10)['items'])==1 and store.last_error is None
    store.db.close()


def test_full_group_parser_and_dispatch(tmp_path,monkeypatch):
    from bridge.sdk import OfficialBackend, EventParser, BridgeWebSocket
    handlers=dict(EventParser._EVENT_HANDLERS)
    monkeypatch.setenv('BRIDGE_GROUP_FULL_MESSAGES','true')
    store=Store(Settings(data=tmp_path,token=TOKEN))
    backend=OfficialBackend(store)
    raw={'id':'full','timestamp':iso(),'group_openid':'g','author':{'member_openid':'member'},
         'content':'  <@!bot> untouched  ','message_type':103,
         'attachments':[{'url':'//example.invalid/image','filename':'x.png'}],
         'msg_elements':[{'msg_idx':'q','content':'quoted','attachments':[{'filename':'q.txt'}]}]}
    asyncio.run(backend.on_message('GROUP_MESSAGE_CREATE',raw))
    msg=store.messages(0,10)['items'][0]
    assert (msg['scene'],msg['chat_id'],msg['user_id'],msg['event_type'])==('group','g','member','GROUP_MESSAGE_CREATE')
    assert msg['content']=='<@!bot> untouched'
    assert msg['attachments'][0]['url']=='https://example.invalid/image'
    assert msg['msg_elements'][0]['attachments'][0]['filename']=='q.txt'
    assert EventParser._EVENT_HANDLERS==handlers
    asyncio.run(backend.on_message('GROUP_MESSAGE_CREATE',{**raw,'id':'missing','group_openid':''}))
    monkeypatch.setenv('BRIDGE_GROUP_FULL_MESSAGES','false')
    asyncio.run(OfficialBackend(store).on_message('GROUP_MESSAGE_CREATE',{**raw,'id':'disabled'}))
    assert len(store.messages(0,10)['items'])==1
    class Callbacks:
        on_message_event=backend.on_message
    ws=BridgeWebSocket(Callbacks())
    ws.full_messages=True
    ws._dispatch_to_main=lambda coro:asyncio.run(coro)
    ws._handle_dispatch('GROUP_MESSAGE_CREATE',{**raw,'id':'dispatch'})
    assert len(store.messages(0,10)['items'])==2
    store.db.close()


def test_version_guard(tmp_path,monkeypatch):
    import bridge.sdk as sdk
    store=Store(Settings(data=tmp_path,token=TOKEN))
    monkeypatch.setattr(sdk,'version',lambda name:'1.2.3')
    with pytest.raises(RuntimeError,match='version must be 1.2.2'):
        sdk.OfficialBackend(store)
    store.db.close()


def test_interaction_callbacks_safe_errors(tmp_path,caplog):
    import logging
    from bridge.sdk import OfficialBackend, BridgeWebSocket
    store=Store(Settings(data=tmp_path,token=TOKEN))
    backend=OfficialBackend(store)
    calls=[]
    class API:
        ensure_token_sync=clear_token=get_gateway_url_sync=lambda self:None
        async def acknowledge_interaction(self,interaction_id,code=0):
            calls.append((interaction_id,code))
            if interaction_id=='fail': raise RuntimeError('secret-token user-content')
    backend.api=API()
    ws=BridgeWebSocket(backend.callbacks())
    ws._dispatch_to_main=lambda coro:asyncio.run(coro)
    ws._handle_dispatch('INTERACTION_CREATE',{'id':'ok'})
    ws._handle_dispatch('INTERACTION_CREATE',{'id':'fail'})
    assert calls==[('ok',0),('fail',0)]
    assert store.last_error=={'stage':'interaction_ack','class':'RuntimeError','close_code':None}
    logging.getLogger('qqbot_agent_sdk.websocket').error('secret-token user-content')
    assert 'secret-token' not in caplog.text and 'user-content' not in caplog.text
    store.report_error('gateway',RuntimeError('secret-token'),4004)
    app=create_app(Settings(data=tmp_path/'http',token=TOKEN),Fake())
    app.state.store.last_error=store.last_error
    with TestClient(app,headers=AUTH) as c:
        assert c.get('/v1/status').json()['last_error']=={'stage':'gateway','class':'RuntimeError','close_code':4004}
    store.db.close()


@pytest.mark.parametrize('scene',['c2c','group'])
@pytest.mark.parametrize('kind,original,expected',[
    ('voice',r'C:\fakepath\audio.ogg','audio.ogg'),('file','../../report.pdf','report.pdf'),
    ('voice','noext','noext.silk'),('file','noext','noext.bin')])
def test_sdk_safe_media_filename(tmp_path,monkeypatch,scene,kind,original,expected):
    from bridge.sdk import OfficialBackend
    from qqbot_agent_sdk.media_loader import MediaUploader
    cfg=Settings(data=tmp_path,token=TOKEN,authorized={scene+':u'})
    app=create_app(cfg,Fake())
    with TestClient(app,headers=AUTH) as c:
        r=c.post('/v1/media',files={'file':(original,b'not-format-validated')})
        assert r.status_code==201
        mid=r.json()['media_id']
        backend=OfficialBackend(app.state.store)
        calls=[]
        async def upload(self,chat_type,chat_id,source,file_type,file_name=None):
            assert chat_type==scene and chat_id=='u' and file_name==expected
            assert '/' not in file_name and '\\' not in file_name
            calls.append(file_name)
            return 'fake-token'
        monkeypatch.setattr(MediaUploader,'upload',upload)
        class API:
            async def post_c2c_message(self,*args): return {'id':'fake'}
            async def post_group_message(self,*args): return {'id':'fake'}
        backend.api=API()
        asyncio.run(backend.send(dict(media_id=mid,media_kind=kind,text='',markdown=False,scene=scene,chat_id='u'),(scene,'u',None)))
        assert calls==[expected]


def test_total_media_ceiling_and_part_cleanup(tmp_path):
    app=create_app(Settings(data=tmp_path,token=TOKEN,max_media=6),Fake())
    store=app.state.store
    leftover=store.media_dir/('f'*32+'.part')
    leftover.write_bytes(b'orphan')
    with TestClient(app,headers=AUTH) as c:
        first=c.post('/v1/media',files={'file':('one.bin',b'abcd')})
        assert first.status_code==201 and not leftover.exists()
        mid=first.json()['media_id']
        assert c.post('/v1/media',files={'file':('two.bin',b'abc')}).status_code==413
        assert store.media_usage()==4 and not list(store.media_dir.glob('*.part'))
        with store.db: store.db.execute('UPDATE media SET expires=0 WHERE id=?',(mid,))
        assert c.post('/v1/media',files={'file':('two.bin',b'abcdef')}).status_code==201
        assert store.db.execute('SELECT filename FROM media_names WHERE id=?',(mid,)).fetchone() is None
        assert store.media_usage()==6


@pytest.mark.parametrize('status,expected',[(400,'failed'),(401,'failed'),(403,'failed'),(408,'unknown'),(500,'unknown')])
def test_external_error_categories_persist(tmp_path,status,expected):
    from bridge.sdk import OfficialBackend
    cfg=Settings(data=tmp_path,token=TOKEN,send_interval=0,authorized={'c2c:u'})
    sdk_store=Store(cfg)
    backend=OfficialBackend(sdk_store)
    backend.run=Fake().run
    backend.connected=backend.api_ready=True
    class API:
        async def post_c2c_message(self,*args):
            raise RuntimeError(f'QQ Bot API error [{status}] code=123 secret-token user-content')
    backend.api=API()
    app=create_app(cfg,backend)
    with TestClient(app,headers=AUTH) as c:
        c.post('/v1/messages/send',json={'idempotency_key':'error','scene':'c2c','chat_id':'u','text':'x'})
        result=terminal(c,'error')
        assert result['status']==expected
        assert 'secret-token' not in str(result) and 'user-content' not in str(result)
    sdk_store.db.close()
    restarted=Store(cfg)
    assert restarted.send_record('error')['status']==expected
    assert 'secret-token' not in str(restarted.send_record('error'))
    restarted.db.close()
