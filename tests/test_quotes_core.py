"""Core quote contract; entirely offline, independent of the SDK DTO adapter."""
import hashlib
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bridge.core import Settings, Store, SendRequest, create_app, iso, resolve_quote, validate_route
from test_bridge import AUTH, TOKEN, Fake, offline_only, terminal


@pytest.fixture
def store(tmp_path):
    instance = Store(Settings(data=tmp_path, token=TOKEN, send_interval=0))
    yield instance
    instance.db.close()


def incoming(store, message_id='ROBOT-passive-id', scene='c2c', chat='u', **extra):
    return store.observe(dict(message_id=message_id, scene=scene, chat_id=chat,
                              user_id='u', timestamp=iso(), content='hello', **extra))


def outgoing(store, key='sent-key', scene='c2c', chat='u', status='sent', result=None):
    body = dict(idempotency_key=key, scene=scene, chat_id=chat, text='out')
    with store.db:
        store.db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)',
                         (key, 'legacy', json.dumps(body), status, iso(), iso(), None,
                          json.dumps(result) if result is not None else None))
        store.db.execute('INSERT INTO send_routes VALUES(?,?,?,NULL)', (key, scene, chat))
    store.index_send(key)
    return key


def error(code, function, *args):
    with pytest.raises(HTTPException) as exc:
        function(*args)
    assert exc.value.status_code == code
    assert 'quote' in exc.value.detail


@pytest.mark.parametrize('fields', [dict(quote_to=0), dict(quote_to=-1), dict(quote_to=True),
                                    dict(quote_to='1'), dict(quote_key=''),
                                    dict(quote_to=1, quote_key='k')])
def test_invalid_request(fields):
    with pytest.raises(ValidationError):
        SendRequest(idempotency_key='k', text='x', **fields)


def test_quote_schema_independent_of_passive_reply():
    body = SendRequest(idempotency_key='k', text='x', reply_to=1, quote_to=2)
    assert body.reply_to == 1 and body.quote_to == 2
    assert SendRequest(idempotency_key='k', text='x', quote_key='out-key').quote_key == 'out-key'


@pytest.mark.parametrize('extra', [dict(reference_index='idx'), dict(msg_idx='idx'),
                                  dict(message_scene={'msg_idx':'idx'}),
                                  dict(message_scene={'ext':['auth_token=never-expose', 'msg_idx=idx']}),
                                  dict(message_scene={'ext':['not-json', '{"msg_idx":"idx"}']})])
def test_resolve_inbound_reference(store, extra):
    seq = incoming(store, **extra)
    assert resolve_quote(store, {'quote_to':seq}, 'c2c', 'u') == 'idx'
    assert resolve_quote(store, {}, 'c2c', 'u') is None
    assert store.db.execute('SELECT inbound_seq FROM reference_lookup WHERE reference_index=?', ('idx',)).fetchone()[0] == seq


def test_no_robot_id_fallback_and_explicit_errors(store):
    seq = incoming(store)
    error(409, resolve_quote, store, {'quote_to':seq}, 'c2c', 'u')
    error(404, resolve_quote, store, {'quote_to':999}, 'c2c', 'u')
    error(409, resolve_quote, store, {'quote_to':seq}, 'c2c', 'other')
    error(409, resolve_quote, store, {'quote_to':seq}, 'group', 'u')
    error(422, resolve_quote, store, {'quote_to':seq}, 'guild', 'u')
    error(422, resolve_quote, store, {'quote_to':seq}, 'dm', 'u')
    error(422, resolve_quote, store, {'quote_to':seq, 'quote_key':'k'}, 'c2c', 'u')


@pytest.mark.parametrize('bad', [None, '', True, {}, [], 'bad\nindex'])
def test_invalid_reference_does_not_become_id(store, bad):
    seq = incoming(store, reference_index=bad, message_scene={'ext':['ROBOT-id', '{"id":"ROBOT-id"}']})
    error(409, resolve_quote, store, {'quote_to':seq}, 'c2c', 'u')


def test_resolve_sent_and_never_result_id(store):
    key = outgoing(store, result={'id':'ROBOT-out', 'ext_info':{'ref_idx':'ref-out'}})
    assert resolve_quote(store, {'quote_key':key}, 'c2c', 'u') == 'ref-out'
    error(409, resolve_quote, store, {'quote_key':key}, 'c2c', 'other')
    error(404, resolve_quote, store, {'quote_key':'missing'}, 'c2c', 'u')
    no_ref = outgoing(store, key='no-ref', result={'id':'ROBOT-out'})
    error(409, resolve_quote, store, {'quote_key':no_ref}, 'c2c', 'u')
    with store.db:
        store.db.execute('DELETE FROM send_routes WHERE key=?', (key,))
    error(409, resolve_quote, store, {'quote_key':key}, 'c2c', 'u')


@pytest.mark.parametrize('status', ['queued', 'sending', 'failed', 'unknown'])
def test_only_successful_sends_can_be_quoted(store, status):
    key = outgoing(store, status=status, result={'ext_info':{'ref_idx':'idx'}})
    error(409, resolve_quote, store, {'quote_key':key}, 'c2c', 'u')
    assert store.db.execute('SELECT 1 FROM reference_lookup').fetchone() is None


def test_passive_routing_stays_independent(store):
    quoted = incoming(store, message_id='old', reference_index='quote-index')
    passive = incoming(store, message_id='new', reference_index='reply-index')
    body = dict(reply_to=passive, quote_to=quoted, text='answer')
    assert validate_route(store, body) == ('c2c', 'u', 'new')
    assert resolve_quote(store, body, 'c2c', 'u') == 'quote-index'
    assert store.reply_count(quoted) == 0
    assert validate_route(store, dict(scene='c2c', chat_id='u', quote_to=quoted)) == ('c2c', 'u', None)
    error(404, validate_route, store, dict(scene='c2c', chat_id='u', quote_to=999))
    error(422, validate_route, store, dict(scene='guild', chat_id='u', quote_to=quoted))


def test_quote_linking_is_scoped_and_non_destructive(store):
    first = incoming(store, reference_index='ref')
    incoming(store, message_id='other', chat='other', reference_index='ref')
    seq = incoming(store, message_id='quote', quote={'source':'explicit', 'reference_index':'ref',
                   'elements':[{'content':'hello', 'author':{'user_id':'u'}, 'attachments':[]}]})
    original = store.db.execute('SELECT payload FROM messages WHERE seq=?', (seq,)).fetchone()[0]
    quote = store.message(seq)['quote']
    assert quote['seq'] == first and quote['reference_index'] == 'ref'
    assert store.db.execute('SELECT payload FROM messages WHERE seq=?', (seq,)).fetchone()[0] == original
    outgoing(store, result={'ext_info':{'ref_idx':'out-ref'}})
    out_quote = incoming(store, message_id='out-quote', quote={'source':'explicit', 'reference_index':'out-ref', 'elements':[]})
    assert store.message(out_quote)['quote']['key'] == 'sent-key'
    unmatched = incoming(store, message_id='unmatched', quote={'source':'explicit', 'reference_index':'unknown', 'elements':[]})
    assert 'seq' not in store.message(unmatched)['quote']


def test_legacy_inference_requires_explicit_marker_and_whitelists_author(store):
    target = incoming(store, reference_index='ref')
    elements = [{'content':'nested', 'author':{'id':'u', 'username':'Name', 'secret':'no'},
                 'attachments':[{'filename':'a'}], 'msg_elements':[{'content':'inner'}]}]
    plain = incoming(store, message_id='plain', message_type=103, msg_elements=elements)
    assert 'quote' not in store.message(plain)
    seq = incoming(store, message_id='explicit', message_reference={'message_id':'ref'}, msg_elements=elements)
    item = store.message(seq)
    assert item['msg_elements'] == elements
    assert item['quote']['source'] == 'explicit'
    assert item['quote']['seq'] == target
    assert item['quote']['elements'][0]['elements'][0]['content'] == 'inner'
    assert 'secret' not in item['quote']['elements'][0]['author']
    seq2 = incoming(store, message_id='explicit2', ref_msg_idx='ref', msg_elements=elements)
    assert store.message(seq2)['quote']['seq'] == target
    seq3 = incoming(store, message_id='explicit3', message_scene={'ext':['ref_msg_idx=ref']}, msg_elements=elements)
    assert store.message(seq3)['quote']['seq'] == target


def test_reference_migration_preserves_legacy_column_counts_and_payloads(store):
    seq = incoming(store, reference_index='in-ref')
    key = outgoing(store, result={'ext_info':{'ref_idx':'out-ref'}})
    before = [tuple(r) for r in store.db.execute('SELECT * FROM messages')]
    sends_before = [tuple(r) for r in store.db.execute('SELECT * FROM sends')]
    with store.db:
        store.db.execute('DROP TABLE reference_lookup')
    store.upgrade()
    assert [tuple(r) for r in store.db.execute('SELECT * FROM messages')] == before
    assert [tuple(r) for r in store.db.execute('SELECT * FROM sends')] == sends_before
    assert len(store.db.execute('PRAGMA table_info(messages)').fetchall()) == 6
    assert len(store.db.execute('PRAGMA table_info(sends)').fetchall()) == 8
    assert len(store.db.execute('SELECT * FROM reference_lookup').fetchall()) == 2
    assert store.db.execute('SELECT inbound_seq FROM reference_lookup WHERE reference_index=?', ('in-ref',)).fetchone()[0] == seq
    assert resolve_quote(store, {'quote_key':key}, 'c2c', 'u') == 'out-ref'
    store.upgrade()
    assert len(store.db.execute('SELECT * FROM reference_lookup').fetchall()) == 2


@pytest.mark.parametrize('legacy', [False, True])
def test_enqueue_idempotency_optional_none_compatibility(tmp_path, legacy):
    fake = Fake()
    fake.connected = fake.api_ready = False
    app = create_app(Settings(data=tmp_path, token=TOKEN, send_interval=0), fake)
    with TestClient(app, headers=AUTH) as client:
        body = dict(idempotency_key='same', scene='c2c', chat_id='u', text='hello')
        assert client.post('/v1/messages/send', json=body).status_code == 202
        if legacy:
            row = app.state.store.db.execute('SELECT payload FROM sends WHERE key=?', ('same',)).fetchone()
            old = json.loads(row[0])
            old.pop('quote_to')
            old.pop('quote_key')
            encoded = json.dumps(old, sort_keys=True, separators=(',', ':'))
            with app.state.store.db:
                app.state.store.db.execute('UPDATE sends SET payload=?,fingerprint=? WHERE key=?',
                                          (encoded, hashlib.sha256(encoded.encode()).hexdigest(), 'same'))
        assert client.post('/v1/messages/send', json=body).status_code == 202
        assert client.post('/v1/messages/send', json={**body, 'quote_to':1}).status_code == 409
        assert client.post('/v1/messages/send', json={**body, 'quote_key':'out'}).status_code == 409


def test_enqueue_validation_history_and_revalidation(tmp_path):
    fake = Fake()
    fake.connected = fake.api_ready = False
    app = create_app(Settings(data=tmp_path, token=TOKEN, send_interval=0), fake)
    with TestClient(app, headers=AUTH) as client:
        store = app.state.store
        seq = incoming(store, reference_index='ref')
        body = dict(idempotency_key='quote', scene='c2c', chat_id='u', text='hello', quote_to=seq)
        assert client.post('/v1/messages/send', json={**body, 'quote_to':999}).status_code == 404
        assert store.db.execute('SELECT COUNT(*) FROM sends').fetchone()[0] == 0
        assert client.post('/v1/messages/send', json=body).status_code == 202
        # The same resolver is used again by queued route validation; losing the
        # index after enqueue must fail, rather than send an unquoted message.
        row = store.db.execute('SELECT payload FROM messages WHERE seq=?', (seq,)).fetchone()
        item = json.loads(row[0])
        item.pop('reference_index')
        with store.db:
            store.db.execute('UPDATE messages SET payload=? WHERE seq=?', (json.dumps(item), seq))
        assert terminal(client, 'quote')['status'] == 'failed'
        assert fake.calls == []
        history = store.session_history('c2c', 'u', 0, 50, 'out')['items']
        assert history[0]['quote_to'] == seq and history[0]['quote_key'] is None
