"""Response projections.

``lite`` is what an agent needs to act: who spoke, what they said, which media
to look at, and whether a reply is still possible. ``full`` keeps the raw
archive shape for debugging. Both are produced from the same stored record, so
there is a single source of truth.
"""


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


def lean_sender(record):
    """Merge the message snapshot and the cached profile into one object."""
    profile = record.get('sender_profile') or {}
    snapshot = record.get('sender') or {}
    out = {'id': record.get('user_id')}
    name = profile.get('username') or snapshot.get('username')
    role = profile.get('member_role') or snapshot.get('member_role')
    avatar = profile.get('avatar') or snapshot.get('avatar')
    if name:
        out['name'] = name
    if role:
        out['role'] = role
    if avatar:
        out['avatar'] = avatar
    return out


def lean_message(record):
    out = {'seq': record.get('seq')}
    if record.get('content'):
        out['text'] = record['content']
    out['from'] = lean_sender(record)
    if record.get('timestamp'):
        out['at'] = record['timestamp']
    attachments = [lean_attachment(a) for a in record.get('attachments') or []]
    if attachments:
        out['attachments'] = attachments
    if record.get('asr_text'):
        out['asr'] = record['asr_text']
    reference = record.get('message_reference') or {}
    if reference.get('message_id'):
        out['quoted'] = reference['message_id']
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
    return out
