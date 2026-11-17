"""Quote adapter contracts, using real SDK DTOs and offline stores only."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from qqbot_agent_sdk.dto import MsgElement

import bridge.core as core
import bridge.sdk as sdk
from bridge.core import Settings, Store, StageFailure, iso
from bridge.quotes import normalize_inbound_quote, parse_reference_metadata, sanitize_elements


@pytest.fixture
def store(tmp_path):
    value = Store(Settings(data=tmp_path, token='offline-test-token-0123456789'))
    yield value
    value.db.close()


def upstream(reference=True):
    ext = ['msg_idx=own==', 'auth_token=secret-scene']
    if reference:
        ext.append('ref_msg_idx=quoted==')
    return dict(id='robot-message-id', timestamp=iso(), group_openid='g',
                author={'member_openid': 'u'}, content='new message', message_type=103,
                message_scene={'ext': ext, 'auth_token': 'secret-scene'},
                msg_elements=[dict(msg_idx='quoted==', content='original message', source='raw_event',
                    author={'member_openid': 'original-author', 'username': 'Alice', 'member_role': 'admin',
                            'bot': False, 'avatar': '//cdn.qq.com/a', 'auth_token': 'secret-author'},
                    attachments=[dict(url='//cdn.qq.com/p', filename='p.png', content_type='image/png',
                                      size=4, width=2, height=2, voice_wav_url='//cdn.qq.com/w',
                                      asr_refer_text='platform transcript', auth_token='secret-media')],
                    msg_elements=[dict(content='nested', author={'id': 'nested-user'},
                                       auth_token='secret-nested')], auth_token='secret-element')])


def test_explicit_metadata_and_partition():
    raw = upstream()
    raw['message_reference'] = {'message_id': 'fallback', 'auth_token': 'secret'}
    assert parse_reference_metadata(raw) == ('own==', 'quoted==')
    quote = normalize_inbound_quote(raw)
    assert quote['reference_index'] == 'quoted=='
    element = quote['elements'][0]
    assert element['author'] == dict(user_id='original-author', username='Alice', member_role='admin',
                                     bot=False, avatar='https://cdn.qq.com/a')
    assert element['source'] == 'raw_event' and element['msg_idx'] == 'quoted=='
    assert element['msg_elements'][0]['author']['user_id'] == 'nested-user'
    assert element['attachments'][0]['asr_text'] == 'platform transcript'
    assert element['attachments'][0]['voice_wav_url'] == 'https://cdn.qq.com/w'
    assert 'secret' not in json.dumps(quote) and 'auth_token' not in json.dumps(quote)
    raw['message_scene']['ext'] = ['msg_idx=own==']
    assert parse_reference_metadata(raw) == ('own==', 'fallback')
    assert normalize_inbound_quote(raw)['reference_index'] == 'fallback'


@pytest.mark.parametrize('kind', [0, 103])
def test_type_and_elements_alone_are_not_quotes(kind):
    raw = upstream(False)
    raw['message_type'] = kind
    assert normalize_inbound_quote(raw) is None
    assert parse_reference_metadata(raw) == ('own==', None)


def test_bounded_tree_has_explicit_markers():
    wide = sanitize_elements([{'content': str(i)} for i in range(50)])
    assert len(wide) == 21 and wide[-1] == {'truncated': True, 'reason': 'max_items'}
    nested = {'content': 'leaf'}
    for _ in range(10):
        nested = {'content': 'prefix', 'elements': [nested]}
    safe = sanitize_elements([nested])
    assert 'max_depth' in json.dumps(safe) and safe[0]['content'] == 'prefix'
    assert len(json.dumps(safe)) < 1000


@pytest.mark.parametrize('explicit', [True, False])
def test_inbound_raw_keeps_authors_nesting_own_index_and_no_secrets(store, explicit):
    backend = sdk.OfficialBackend(store)
    raw = upstream(explicit)
    if explicit:
        raw['message_reference'] = {'message_id': 'explicit-id', 'auth_token': 'secret-reference',
                                    'ignore_get_message_error': True}
    asyncio.run(backend._observe_message('GROUP_AT_MESSAGE_CREATE', raw))
    item = store.messages(0, 10)['items'][0]
    assert item['reference_index'] == 'own==' and item['message_type'] == 103
    assert item['msg_elements'][0]['author']['username'] == 'Alice'
    assert item['msg_elements'][0]['msg_elements'][0]['content'] == 'nested'
    assert ('quote' in item) is explicit
    assert item['message_reference'] == ({'message_id': 'explicit-id'} if explicit else None)
    if explicit:
        assert item['quote']['reference_index'] == 'quoted=='
    assert 'auth_token' not in json.dumps(item) and 'secret' not in json.dumps(item)


def test_inbound_sdk_fallback(store, monkeypatch):
    backend = sdk.OfficialBackend(store)
    event = SimpleNamespace(message_id='sdk-id', user_id='u', user_name='', chat_scope='c2c',
                            chat_id='u', attachments=[], content='x', timestamp=iso(),
                            msg_elements=[MsgElement(msg_idx='sdk-index', content='SDK fallback')])
    monkeypatch.setattr(sdk.EventParser, 'parse', lambda *args: event)
    asyncio.run(backend._observe_message('C2C_MESSAGE_CREATE',
                {'author': {}, 'message_reference': {'message_id': 'q'}}))
    item = store.messages(0, 10)['items'][0]
    assert item['quote']['elements'][0]['content'] == 'SDK fallback'


@pytest.mark.parametrize('scene', ['c2c', 'group'])
@pytest.mark.parametrize('kind', ['text', 'markdown', 'image', 'voice', 'video', 'file'])
@pytest.mark.parametrize('quoted', [True, False])
def test_real_dto_reference_for_all_types_and_passive_not_autoquoted(store, monkeypatch, scene, kind, quoted):
    backend = sdk.OfficialBackend(store)
    seen = []
    class API:
        async def post_c2c_message(self, chat, msg):
            seen.append(msg.to_dict())
            return {'id': 'out'}
        post_group_message = post_c2c_message
    backend.api = API()
    async def validate(url, hosts):
        return url
    async def upload(*args, **kwargs):
        return 'fake-media-token'
    monkeypatch.setattr(sdk, 'validate_public_media_url', validate)
    monkeypatch.setattr(backend, '_upload_media', upload)
    seq = store.observe(dict(message_id='robot-in', scene=scene, chat_id='target', user_id='u',
                             timestamp=iso(), reference_index='platform-index=='))
    body = dict(text='hello', markdown=kind == 'markdown', reply_to=seq, _msg_seq=2)
    if quoted:
        body['quote_to'] = seq
    if kind not in ('text', 'markdown'):
        body.update(media_url='https://cdn.qq.com/fake', media_kind=kind)
    asyncio.run(backend.send(body, (scene, 'target', 'robot-in')))
    dto = seen[0]
    assert dto['msg_id'] == 'robot-in' and dto['msg_seq'] == 2
    assert dto.get('message_reference') == ({'message_id': 'platform-index=='} if quoted else None)
    assert dto['msg_type'] == (0 if kind == 'text' else 2 if kind == 'markdown' else 7)


def test_quote_key_with_fake_store_and_real_resolver():
    class Cursor:
        def __init__(self, value):
            self.value = value
        def fetchone(self):
            return self.value
    class DB:
        def execute(self, sql, args):
            assert args == ('previous',)
            return Cursor({'scene': 'c2c', 'chat_id': 'u'} if 'send_routes' in sql else
                          {'status': 'sent', 'result': json.dumps({'ext_info': {'ref_idx': 'out-index=='}})})
    backend = sdk.OfficialBackend.__new__(sdk.OfficialBackend)
    backend.store = SimpleNamespace(db=DB())
    seen = []
    class API:
        async def post_c2c_message(self, chat, msg):
            seen.append(msg.to_dict())
    backend.api = API()
    asyncio.run(backend.send(dict(text='quote previous', markdown=False, quote_key='previous'), ('c2c', 'u', None)))
    assert seen[0]['message_reference'] == {'message_id': 'out-index=='}


@pytest.mark.parametrize('after_upload', [False, True])
def test_local_resolution_failure_is_definite_validate(store, monkeypatch, after_upload):
    backend = sdk.OfficialBackend(store)
    calls = []
    def resolve(*args):
        calls.append('resolve')
        if not after_upload or calls.count('resolve') > 1:
            raise HTTPException(409, 'secret detail')
        return 'index'
    async def validate(url, hosts):
        return url
    async def upload(*args, **kwargs):
        calls.append('upload')
        return 'token'
    monkeypatch.setattr(core, 'resolve_quote', resolve)
    monkeypatch.setattr(core, 'validate_route', lambda *args: None)
    monkeypatch.setattr(sdk, 'validate_public_media_url', validate)
    monkeypatch.setattr(backend, '_upload_media', upload)
    with pytest.raises(StageFailure) as caught:
        asyncio.run(backend.send(dict(text='x', markdown=False, quote_to=1,
                    media_url='https://cdn.qq.com/a', media_kind='image'), ('c2c', 'u', None)))
    assert caught.value.stage == 'validate' and not caught.value.uncertain
    assert caught.value.reason == 'quote target unavailable or invalid'
    assert ('upload' in calls) is after_upload


@pytest.mark.parametrize('scene', ['guild', 'dm'])
def test_no_quote_on_guild_or_dm(store, scene):
    backend = sdk.OfficialBackend(store)
    with pytest.raises(StageFailure) as caught:
        asyncio.run(backend.send(dict(text='x', markdown=False, quote_key='any'), (scene, 'u', None)))
    assert caught.value.stage == 'validate' and not caught.value.uncertain


def test_only_matching_snapshot_is_quoted_when_context_is_mixed():
    raw=upstream()
    raw['msg_elements'].append({'msg_idx':'different-index','content':'unrelated context'})
    quoted=normalize_inbound_quote(raw)
    assert len(quoted['elements'])==1
    assert quoted['elements'][0]['msg_idx']=='quoted=='
