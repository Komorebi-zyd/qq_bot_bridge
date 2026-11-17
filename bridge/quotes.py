"""Bounded, allowlisted quote metadata from untrusted gateway payloads.

Indexes are opaque platform strings, not message IDs or bridge sequence numbers.
No message_scene payload (in particular auth_token) leaves this module.
"""

MAX_DEPTH = 4
MAX_ITEMS = 20


def _index(value):
    if isinstance(value, str) and value:
        return value
    if type(value) is int:
        return str(value)
    return None


def parse_reference_metadata(raw):
    """Return (own_index, quote_index), accepting only explicit reference metadata."""
    if not isinstance(raw, dict):
        return None, None
    scene = raw.get('message_scene')
    ext = scene.get('ext') if isinstance(scene, dict) else None
    metadata = {}
    if isinstance(ext, list):
        for entry in ext:
            if not isinstance(entry, str):
                continue
            key, separator, value = entry.partition('=')
            if separator and key in ('msg_idx', 'ref_msg_idx') and value:
                metadata[key] = value
    reference = raw.get('message_reference')
    fallback = _index(reference.get('message_id')) if isinstance(reference, dict) else None
    return metadata.get('msg_idx'), metadata.get('ref_msg_idx') or fallback


def _url(value):
    return 'https:' + value if value.startswith('//') else value


def _author(raw):
    if not isinstance(raw, dict):
        return None
    result = {}
    for key in ('user_id', 'user_openid', 'member_openid', 'id'):
        value = _index(raw.get(key))
        if value is not None:
            result['user_id'] = value
            break
    for key in ('username', 'member_role', 'avatar'):
        value = raw.get(key)
        if isinstance(value, str) or type(value) is int:
            result[key] = _url(value) if key == 'avatar' and isinstance(value, str) else value
    if type(raw.get('bot')) is bool:
        result['bot'] = raw['bot']
    return result or None


def sanitize_elements(raw):
    """Allowlist raw/SDK elements, sharing a 20-item budget across the whole tree.

    Nesting stops after four element levels. A visible truncation marker is
    appended at each cut, retaining the already-sanitized prefix of a quote.
    """
    remaining = MAX_ITEMS

    def marker(reason):
        return {'truncated': True, 'reason': reason}

    def collection(values, depth, attachment=False):
        nonlocal remaining
        if not isinstance(values, (list, tuple)):
            return []
        result = []
        if values and depth >= MAX_DEPTH:
            return [marker('max_depth')]
        for value in values:
            if not isinstance(value, dict):
                continue
            if remaining <= 0:
                result.append(marker('max_items'))
                break
            remaining -= 1
            item = {}
            if attachment:
                for key in ('url', 'voice_wav_url', 'filename', 'content_type'):
                    if isinstance(value.get(key), str):
                        item[key] = _url(value[key]) if key.endswith('url') else value[key]
                for key in ('size', 'width', 'height'):
                    if type(value.get(key)) is int:
                        item[key] = value[key]
                asr = value.get('asr_refer_text', value.get('asr_text'))
                if isinstance(asr, str):
                    item['asr_text'] = asr
            else:
                for key in ('content', 'source'):
                    if isinstance(value.get(key), str):
                        item[key] = value[key]
                index = _index(value.get('msg_idx'))
                if index is not None:
                    item['msg_idx'] = index
                author = _author(value.get('author'))
                if author:
                    item['author'] = author
                if isinstance(value.get('attachments'), (list, tuple)):
                    item['attachments'] = collection(value['attachments'], depth, attachment=True)
                for key in ('msg_elements', 'elements'):
                    if isinstance(value.get(key), (list, tuple)):
                        item[key] = collection(value[key], depth + 1)
            result.append(item)
        return result

    return collection(raw, 0)


def normalize_inbound_quote(raw):
    """Return an explicit quote's reference_index and sanitized elements, or None.

    message_type=103 alone is contextual evidence, never proof of a quote.
    """
    _, reference = parse_reference_metadata(raw)
    if reference is None:
        return None
    elements = sanitize_elements(raw.get('msg_elements'))
    # Explicit references may arrive alongside unrelated context elements.
    # Prefer the matching snapshot when the platform provides element indices.
    matched = [element for element in elements if element.get('msg_idx') == reference]
    return {'reference_index': reference, 'elements': matched or elements}
