"""Response projections.

``lite`` is what an agent needs to act: who spoke, what they said, which media
to look at, and whether a reply is still possible. ``full`` keeps the raw
archive shape for debugging. Both are produced from the same stored record, so
there is a single source of truth.
"""

import base64
import json
import re
from urllib.parse import urlsplit

MENTION_ALL = re.compile(r'<@all>', re.I)
MENTION_ID = re.compile(r'<@([0-9A-Za-z]+)>')
FACE = re.compile(r'<faceType=(\d+)\s*,\s*faceId="?\d+"?\s*(?:,\s*ext="([^"]*)")?\s*/?>')


def _face_label(ext):
    """QQ puts the sticker's name inside a base64 JSON blob."""
    if not ext:
        return None
    try:
        payload = json.loads(base64.b64decode(ext, validate=True).decode('utf-8'))
    except Exception:
        return None
    text = payload.get('text') if isinstance(payload, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def normalize_text(content, mentions=None):
    """Turn QQ markup into something an agent can read.

    ``<@openid>`` becomes ``[@name]``, ``<@all>`` becomes ``[@全体成员]`` and
    sticker tags become ``[表情:名字]`` (or ``[表情]`` when unnamed).
    """
    if not content:
        return content
    names = {}
    for item in mentions or []:
        uid = item.get('user_id') or item.get('id')
        if uid:
            names[str(uid)] = item.get('username') or item.get('name')

    text = MENTION_ALL.sub('[@全体成员]', content)

    def mention(match):
        uid = match.group(1)
        name = names.get(uid)
        return f'[@%s]' % (name or '成员')

    text = MENTION_ID.sub(mention, text)

    def face(match):
        label = _face_label(match.group(2))
        return '[表情:%s]' % label if label else '[表情]'

    return FACE.sub(face, text)


def _kind(content_type, filename=''):
    value = (content_type or '').lower()
    if value.startswith('image/'):
        return 'image'
    if value.startswith('video/'):
        return 'video'
    if value == 'voice' or value.startswith('audio/'):
        return 'voice'
    if value == 'file':
        return 'file'
    return 'file'


def lean_attachment(item):
    out = {'kind': _kind(item.get('content_type'), item.get('filename'))}
    for key in ('filename', 'url', 'size', 'width', 'height', 'voice_wav_url'):
        value = item.get(key)
        if value not in (None, '', 0):
            out[key] = value
    return out


def avatar_url(value):
    """Use only the supplied avatar URL; an OpenID is not a URL source."""
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        return None
    if value.startswith('//'):
        value = 'https:' + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ('http', 'https') and parsed.hostname and parsed.username is None:
            return value
    except ValueError:
        pass
    return None


def info_view(scene, chat_id, profile=None, group=None):
    out = {'scene': scene, 'chat_id': chat_id}
    if profile is not None:
        out['user_id'] = profile['user_id']
        fields = (('username', 'name'), ('member_role', 'role'), ('bot', 'bot'), ('updated_at', 'updated_at'))
        source = profile
    else:
        source = group or {}
        out['info_status'] = ('unavailable' if source.get('unavailable') else 'cached') if group is not None else 'not_cached'
        fields = (('group_name', 'name'), ('group_member_num', 'member_count'),
                  ('group_finger_memo', 'description'), ('group_class_text', 'category'), ('group_tags', 'tags'))
    for key, alias in fields:
        value = source.get(key)
        if value is not None and value != '' and value != []:
            out[alias] = value
    avatar = avatar_url(source.get('avatar'))
    if avatar:
        out['avatar'] = avatar
    return out


def lean_sender(record):
    """Merge the message snapshot and the cached profile into one object."""
    profile = record.get('sender_profile') or {}
    snapshot = record.get('sender') or {}
    out = {'id': record.get('user_id')}
    name = profile.get('username') or snapshot.get('username')
    role = profile.get('member_role') or snapshot.get('member_role')
    if name:
        out['name'] = name
    if role:
        out['role'] = role
    return out



def lean_quote_element(element):
    """Project quoted content without repeating platform indices or metadata."""
    out = {}
    author = element.get('author') or {}
    sender = {}
    uid = author.get('user_id') or author.get('member_openid') or author.get('user_openid') or author.get('id')
    if uid:
        sender['id'] = uid
    if author.get('username'):
        sender['name'] = author['username']
    if sender:
        out['from'] = sender
    text = normalize_text(element.get('content') or element.get('text'), element.get('mentions'))
    if text and text.strip():
        out['text'] = text
    raw_attachments = [a for a in element.get('attachments') or [] if isinstance(a, dict)]
    attachments = [lean_attachment(a) for a in raw_attachments if not a.get('truncated')]
    if attachments:
        out['attachments'] = attachments
    if any(a.get('truncated') for a in raw_attachments):
        out['truncated'] = True
    if element.get('asr_text'):
        out['asr'] = element['asr_text']
    children = [lean_quote_element(e) for e in element.get('elements') or element.get('msg_elements') or [] if isinstance(e, dict)]
    children = [c for c in children if c]
    if children:
        out['elements'] = children
    if element.get('truncated'):
        out['truncated'] = True
    return out


def lean_quote(record):
    quote = record.get('quote')
    # Legacy archives may have a reference marker and SDK-parsed elements.
    if not isinstance(quote, dict):
        reference = record.get('message_reference') or {}
        if not reference.get('message_id'):
            return None
        quote = {'elements':record.get('msg_elements') or []}
    elements = [lean_quote_element(e) for e in quote.get('elements') or [] if isinstance(e, dict)]
    elements = [e for e in elements if e]
    result = {}
    if len(elements) == 1:
        result.update(elements[0])
    elif elements:
        result['elements'] = elements
    for key in ('seq', 'key'):
        if quote.get(key) is not None:
            result[key] = quote[key]
    if quote.get('truncated'):
        result['truncated'] = True
    # An explicit unresolved reference is meaningful even when no snapshot arrived.
    return result or {'available':False}


def lean_message(record):
    out = {'seq': record.get('seq')}
    text = normalize_text(record.get('content'), record.get('mentions'))
    if text and text.strip():
        out['text'] = text
    out['from'] = lean_sender(record)
    if record.get('timestamp'):
        out['at'] = record['timestamp']
    attachments = [lean_attachment(a) for a in record.get('attachments') or []]
    if attachments:
        out['attachments'] = attachments
    if record.get('asr_text'):
        out['asr'] = record['asr_text']
    quote = lean_quote(record)
    if quote is not None:
        out['quote'] = quote
    if record.get('acked'):
        out['acked'] = True
    out['can_reply'] = bool(record.get('can_reply'))
    out['replies_left'] = record.get('replies_remaining')
    if record.get('reply_deadline'):
        out['reply_deadline'] = record['reply_deadline']
    return out


def lean_outbound(entry):
    out = {'history_seq': entry.get('history_seq'), 'direction': 'out',
           'key': entry.get('idempotency_key'), 'status': entry.get('status')}
    if entry.get('text'):
        out['text'] = entry['text']
    if entry.get('media_kind'):
        out['media_kind'] = entry['media_kind']
    if entry.get('filename'):
        out['filename'] = entry['filename']
    if entry.get('reply_to'):
        out['reply_to'] = entry['reply_to']
    if entry.get('quote_to') is not None:
        out['quote_to'] = entry['quote_to']
    if entry.get('quote_key'):
        out['quote_key'] = entry['quote_key']
    if entry.get('at') or entry.get('timestamp'):
        out['at'] = entry.get('at') or entry.get('timestamp')
    return out


def lean_page(page, scene=None, chat_id=None):
    """Project a message page and hoist a shared chat block out of every item."""
    items = []
    chats = set()
    for record in page.get('items') or []:
        if record.get('direction') == 'out':
            items.append(lean_outbound(record))
            continue
        chats.add((record.get('scene'), record.get('chat_id')))
        entry = lean_message(record)
        if record.get('history_seq'):
            entry['history_seq'] = record['history_seq']
            entry['direction'] = 'in'
        items.append(entry)
    result = {'items': items, 'next_cursor': page.get('next_cursor')}
    if scene and chat_id:
        result['chat'] = {'scene': scene, 'id': chat_id}
    elif len(chats) == 1:
        only_scene, only_chat = next(iter(chats))
        result['chat'] = {'scene': only_scene, 'id': only_chat}
    elif chats:
        result['chat_count'] = len(chats)
    return result


def lean_send(record):
    out = {'key': record.get('idempotency_key'), 'status': record.get('status')}
    if record.get('stage'):
        out['stage'] = record['stage']
    if record.get('msg_seq'):
        out['msg_seq'] = record['msg_seq']
    if record.get('error'):
        out['error'] = record['error']
    codes = record.get('error_details') or {}
    if codes:
        out['codes'] = codes
    return out


def lean_session(item):
    out = {'scene': item.get('scene'), 'id': item.get('chat_id')}
    group = item.get('group') or {}
    if group.get('group_name'):
        out['group_name'] = group['group_name']
    if group.get('group_member_num'):
        out['members'] = group['group_member_num']
    names = [{'id': n.get('user_id'), 'name': n['username']}
             for n in item.get('names') or [] if n.get('username')]
    if names:
        out['people'] = names
    if item.get('last_seq'):
        out['last_seq'] = item['last_seq']
    return out


def lean_capabilities(caps):
    """Only what the agent must know before acting, in short keys."""
    scenes = {}
    for name, info in (caps.get('scenes') or {}).items():
        # Only the scenes this bridge can actually send media in; guild/DM are
        # text-only edge cases and stay visible via detail=full.
        if not info.get('media_kinds'):
            continue
        scenes[name] = {'reply_window': info['reply_window_seconds'],
                        'replies': info['max_replies'],
                        'media': info['media_kinds']}
    limits = caps.get('limits') or {}
    out = {
        'scenes': scenes,
        'limits': {
            'file_mb': round(limits.get('max_file_bytes', 0) / 1048576, 1),
            'inline_mb': round(limits.get('max_inline_media_bytes', 0) / 1048576, 1),
        },
    }
    media = caps.get('media_url') or {}
    if media.get('allowed_hosts'):
        out['media_hosts'] = media['allowed_hosts']
    if caps.get('proactive'):
        out['proactive'] = True
    if (caps.get('features') or {}).get('quote_send'):
        out['quotes'] = True
    return out
