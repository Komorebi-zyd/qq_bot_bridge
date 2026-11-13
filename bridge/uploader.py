"""QQ rich-media upload.

Two reasons this lives outside the SDK helper:

* the SDK collapses a failed upload into a plain ``RuntimeError`` and drops the
  platform ``code``, so 850026 ("download failed") is indistinguishable from
  850019 ("unsupported format") in logs;
* the official docs recommend falling back to a local copy when the platform
  cannot fetch a URL, which needs the download path anyway.
"""
import asyncio
import tempfile
from pathlib import Path

from qqbot_agent_sdk.constants import API_BASE
from qqbot_agent_sdk.media_loader import MediaUploader

UPLOAD_REASONS = {
    850018: 'robot or group is muted',
    850019: 'unsupported media format for this type',
    850026: 'QQ could not download the media URL',
    850027: 'platform timed out fetching the media',
    850031: 'media exceeds the platform size limit',
    10000: 'platform rejected the request parameters',
    40093001: 'platform upload failed and can be retried',
    40093002: 'daily upload quota reached',
}

# Codes where the media itself was rejected, so retrying with our own copy is
# pointless. Everything else (including codes not documented yet) is treated as
# "the platform could not obtain the bytes" and gets one local retry.
NON_RETRYABLE_CODES = frozenset({850018, 850019, 850031})

CHUNK = 65536


class UploadError(Exception):
    """Carries only numeric platform metadata plus a fixed reason string."""

    def __init__(self, status_code=None, bizcode=None):
        self.status_code = status_code if type(status_code) is int else None
        self.bizcode = bizcode if type(bizcode) is int else None
        self.reason = UPLOAD_REASONS.get(self.bizcode)
        if self.reason is None and self.bizcode is not None:
            self.reason = f'platform rejected the upload (code {self.bizcode})'
        self.can_retry_locally = self.bizcode not in NON_RETRYABLE_CODES
        super().__init__(self.reason or 'upload failed')


def _endpoint(scene, chat_id):
    if scene == 'c2c':
        return f'{API_BASE}/v2/users/{chat_id}/files'
    return f'{API_BASE}/v2/groups/{chat_id}/files'


async def _post(api, http, scene, chat_id, body, timeout=30):
    token = await api.ensure_token()
    response = await asyncio.wait_for(http.post(
        _endpoint(scene, chat_id),
        headers={'Authorization': f'QQBot {token}', 'Content-Type': 'application/json'},
        json=body,
    ), timeout)
    if response.status_code >= 400:
        bizcode = None
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and type(payload.get('code')) is int:
            bizcode = payload['code']
        raise UploadError(response.status_code, bizcode)
    payload = response.json()
    info = payload.get('file_info') if isinstance(payload, dict) else None
    if not info:
        raise UploadError(response.status_code, None)
    return str(info)


async def upload_by_url(api, http, scene, chat_id, url, file_type, file_name=None):
    """Let the QQ platform fetch the URL, keeping platform error codes."""
    body = {'file_type': file_type, 'url': url, 'srv_send_msg': False}
    if file_name:
        body['file_name'] = file_name
    return await _post(api, http, scene, chat_id, body)


async def download_media(http, url, max_bytes, timeout=60):
    """Fetch the media locally for the documented fallback path."""
    async with http.stream('GET', url, follow_redirects=True, timeout=timeout) as response:
        response.raise_for_status()
        declared = response.headers.get('content-length')
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError('media larger than the configured limit')
        buffer = bytearray()
        async for chunk in response.aiter_bytes(CHUNK):
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise ValueError('media larger than the configured limit')
    if not buffer:
        raise ValueError('empty media body')
    return bytes(buffer)


async def upload_local_copy(api, http, scene, chat_id, data, file_type, file_name):
    """Chunked upload of bytes we downloaded ourselves."""
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=Path(file_name or 'media').suffix[:20])
    try:
        handle.write(data)
        handle.close()
        uploader = MediaUploader(api, http, log_tag='QQBridge')
        return await uploader.upload(scene, chat_id, handle.name, file_type, file_name)
    finally:
        Path(handle.name).unlink(missing_ok=True)
