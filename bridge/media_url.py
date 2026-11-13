"""Public HTTPS media references; validation never downloads the referenced object."""
import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit


def validate_media_url(url, allowed_hosts=None):
    """Syntactic validation with deliberately URL-free errors (signed URLs are secrets)."""
    try:
        if not isinstance(url, str) or len(url) > 8192 or re.search(r'[\s\\\x00-\x1f\x7f]', url):
            raise ValueError
        parts = urlsplit(url)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        if parts.scheme != 'https' or not host or parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.port not in (None, 443) or parts.fragment or host.endswith('.'):
            raise ValueError
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or any(host == d or host.endswith('.'+d) for d in ('localhost','local','internal','lan','home','localdomain','test','invalid')):
                raise ValueError
            if len(host)>253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
                raise ValueError
        else:
            if not addr.is_global or addr.is_multicast:
                raise ValueError
        if allowed_hosts and host not in {h.strip().lower() for h in allowed_hosts if h.strip()}:
            raise ValueError
        return host
    except (ValueError, UnicodeError):
        raise ValueError('media URL must reference an allowed public HTTPS host') from None


async def validate_public_media_url(url, allowed_hosts=None):
    host = validate_media_url(url, allowed_hosts)
    try:
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,443,type=socket.SOCK_STREAM), 10)
        if not answers or any(not ipaddress.ip_address(a[4][0]).is_global or ipaddress.ip_address(a[4][0]).is_multicast for a in answers):
            raise ValueError
    except (OSError, ValueError, asyncio.TimeoutError):
        raise ValueError('media URL host must resolve only to public addresses') from None
    return url
