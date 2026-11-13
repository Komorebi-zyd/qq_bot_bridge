"""Media URL handling.

The QQ platform fetches the media itself, so the bridge only enforces what the
platform requires of the link: an HTTPS direct URL. No local fetch, no DNS
pre-check and no private-address filtering — those would block links that QQ
would happily accept, without protecting anything on our side.
"""
import re
from urllib.parse import urlsplit


def validate_media_url(url, allowed_hosts=None):
    """Return the host for an HTTPS media URL, or raise ValueError.

    Errors never echo the URL because signed CDN links carry credentials.
    """
    try:
        if not isinstance(url, str) or not url or len(url) > 8192:
            raise ValueError
        if re.search(r'[\s\x00-\x1f\x7f]', url):
            raise ValueError
        parts = urlsplit(url)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        if parts.scheme != 'https' or not host or parts.username is not None or parts.password is not None:
            raise ValueError
        if allowed_hosts and host not in {h.strip().lower() for h in allowed_hosts if h.strip()}:
            raise ValueError
        return host
    except (ValueError, UnicodeError):
        raise ValueError('media_url must be an HTTPS link to the media file') from None


async def validate_public_media_url(url, allowed_hosts=None):
    """Compatibility wrapper: same rules, kept async for existing call sites."""
    validate_media_url(url, allowed_hosts)
    return url
