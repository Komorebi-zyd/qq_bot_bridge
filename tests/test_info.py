"""Avatars are on-demand profile data, not repeated message content."""
import json

import pytest
from fastapi.testclient import TestClient

from bridge.core import iso
from bridge.views import avatar_url
from test_mcp import AUTH, make_app


def observe(store, message_id, scene='group', chat_id='g', user_id='u', **sender):
    return store.observe(dict(message_id=message_id, scene=scene, chat_id=chat_id,
                              user_id=user_id, timestamp=iso(), content='hi',
                              sender=sender, attachments=[{'content_type':'image/png',
                              'url':'https://cdn.example/attachment.png'}]))


def test_200_lite_messages_keep_identity_and_media_without_avatar(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    with TestClient(app, headers=AUTH) as client:
        store = app.state.store
        avatar = 'https://cdn.example/avatar?key=' + 'a' * 96
        assert len(avatar) == 127
        for i in range(200):
            seq = observe(store, str(i), username='Tester', member_role='owner', avatar=avatar)
        for endpoint in ('/v1/messages', '/v1/sessions/group/g/messages', '/v1/sessions/group/g/history'):
            response = client.get(endpoint, params={'detail':'lite', 'limit':200})
            assert response.status_code == 200
            page = response.json()
            assert page['next_cursor'] == seq and len(page['items']) == 200
            assert page['chat'] == {'scene':'group', 'id':'g'}
            assert all(m['from'] == {'id':'u', 'name':'Tester', 'role':'owner'} for m in page['items'])
            assert all(m['attachments'][0]['url'] == 'https://cdn.example/attachment.png' for m in page['items'])
            assert avatar not in response.text
            prior_items = [{**m, 'from':{**m['from'], 'avatar':avatar}} for m in page['items']]
            encode = lambda x: json.dumps(x, ensure_ascii=False, separators=(',', ':'))
            assert len(encode({**page, 'items':prior_items})) - len(encode(page)) == 200 * (127 + len(',"avatar":""'))
        full = client.get('/v1/sessions/group/g/messages', params={'detail':'full','limit':1}).json()['items'][0]
        assert full['sender']['avatar'] == full['sender_profile']['avatar'] == avatar
        result = client.get('/v1/info', params={'scene':'group','chat_id':'g','user_id':'u'}).json()
        assert result['avatar'] == avatar and result['name'] == 'Tester' and result['role'] == 'owner'
        # Blank incoming avatar does not erase the last known URL.
        observe(store, 'blank', avatar='')
        assert client.get('/v1/info?scene=group&chat_id=g&user_id=u').json()['avatar'] == avatar


def test_info_scope_and_optional_group_avatar(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    with TestClient(app, headers=AUTH) as client:
        store = app.state.store
        observe(store,'private',scene='c2c',chat_id='u',username='Private',avatar='//cdn.example/private.png',bot=False)
        observe(store,'group',username='Group',member_role='member',avatar='https://cdn.example/user-in-group.png')
        private = client.get('/v1/info?scene=c2c&chat_id=u').json()
        assert private['user_id'] == 'u' and private['name'] == 'Private' and private['bot'] is False
        assert private['avatar'] == 'https://cdn.example/private.png'
        assert client.get('/v1/info?scene=group&chat_id=g&user_id=u').json()['name'] == 'Group'
        assert client.get('/v1/info?scene=group&chat_id=other&user_id=u').status_code == 404
        assert client.get('/v1/info?scene=group&chat_id=other').status_code == 404
        assert client.get('/v1/info?scene=group&chat_id=g').json() == {'scene':'group','chat_id':'g','info_status':'not_cached'}
        store.set_group_info('g',{'unavailable':True})
        assert client.get('/v1/info?scene=group&chat_id=g').json()['info_status'] == 'unavailable'
        store.set_group_info('g',{'group_name':'Book club','group_member_num':12,'group_finger_memo':'Read together',
                                  'group_class_text':'Reading','group_tags':['Books']})
        info = client.get('/v1/info?scene=group&chat_id=g').json()
        assert info == {'scene':'group','chat_id':'g','info_status':'cached','name':'Book club','member_count':12,
                        'description':'Read together','category':'Reading','tags':['Books']}
        store.set_group_info('g',{'group_name':'Book club','avatar':'https://cdn.example/group.png'})
        assert client.get('/v1/info?scene=group&chat_id=g').json()['avatar'] == 'https://cdn.example/group.png'
        assert 'avatar' not in client.get('/v1/sessions?detail=lite').text
        assert client.get('/v1/info?scene=guild&chat_id=g').status_code == 422
        assert client.get('/v1/info?scene=group&chat_id=').status_code == 422
        assert client.get('/v1/info?scene=group&chat_id=g', headers={'Authorization':'Bearer wrong'}).status_code == 401


@pytest.mark.parametrize('value', ['', None, 123, '12345', 'file:///abc', 'https://user:pass@example.org/a', 'https://['])
def test_avatar_url_requires_an_actual_http_url(value):
    assert avatar_url(value) is None
