#!/usr/bin/env python3
"""Once-only HTTP CLI for the QQ official cloud bridge. Python standard library only.
No daemon, model calls, automatic sends, or credentials stored on disk.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected host.


def connection():
    base = os.environ.get("QQ_BRIDGE_URL", "").strip().rstrip("/")
    token = os.environ.get("QQ_BRIDGE_TOKEN", "").strip()
    if not base or not token:
        raise ValueError("Set QQ_BRIDGE_URL and QQ_BRIDGE_TOKEN in your private environment.")
    p = urllib.parse.urlsplit(base)
    local = p.hostname in ("localhost", "127.0.0.1", "::1")
    if p.scheme != "https" and not (p.scheme == "http" and local):
        raise ValueError("HTTPS required except loopback HTTP for offline tests.")
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("Invalid base URL; do not embed credentials, query, or fragment.")
    if p.path.endswith("/mcp"):
        raise ValueError("This is REST HTTP, not MCP. Remove /mcp from QQ_BRIDGE_URL.")
    return base, token


def request(method, path, payload=None, *, raw=None, content_type=None):
    base, token = connection()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if raw is None and payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=raw, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=60) as response:
            content = response.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("Response too large; reduce page limit.")
            return json.loads(content.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not include request headers/credentials in output.
        text = exc.read(65536).decode("utf-8", errors="replace")
        try:
            detail = json.loads(text)
        except json.JSONDecodeError:
            detail = {"message": "Non-JSON HTTP response"}
        return {"ok": False, "http_status": exc.code, "detail": detail}
    except urllib.error.URLError:
        raise ValueError("Network request failed. For send, query the SAME idempotency key before retrying.") from None


def upload_file(path_str):
    """Upload local file bytes as multipart; it never triggers a QQ send."""
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise ValueError("Upload file does not exist or is not a regular file.")
    limit = int(os.environ.get("QQ_BRIDGE_UPLOAD_MAX_BYTES", str(20 * 1024 * 1024)))
    if limit < 1 or limit > 100 * 1024 * 1024:
        raise ValueError("Client upload limit must be between 1 byte and 100 MiB.")
    if path.stat().st_size > limit:
        raise ValueError("File exceeds client upload limit; check server limits before adjusting it.")
    with path.open("rb") as fh:
        blob = fh.read(limit + 1)
    if len(blob) > limit:
        raise ValueError("File grew beyond upload limit.")
    if not blob:
        raise ValueError("Cannot upload an empty file.")
    boundary = "qqbridge-" + uuid.uuid4().hex
    extension = path.suffix.lower()
    if not extension[1:].isalnum() or len(extension) > 12:
        extension = ".bin"
    name = "upload" + extension
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("ascii")
    data = prefix + blob + f"\r\n--{boundary}--\r\n".encode("ascii")
    return request("POST", "/v1/media", raw=data, content_type=f"multipart/form-data; boundary={boundary}")


def add_page_arguments(parser):
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="Show HTTP and QQ gateway readiness")
    sub.add_parser("capabilities", help="Show enabled bridge features and limits")
    sub.add_parser("sessions", help="List sessions observed by this bridge")
    inbox = sub.add_parser("inbox", help="Pull inbound messages by cursor; never auto-ack or send")
    inbox.add_argument("--after", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--scene", choices=["c2c", "group", "guild", "dm"])
    inbox.add_argument("--chat-id")
    history = sub.add_parser("history", help="Read saved all-direction session history using history_seq")
    history.add_argument("--scene", choices=["c2c", "group", "guild", "dm"], required=True)
    history.add_argument("--chat-id", required=True)
    history.add_argument("--after", type=int, default=0, help="history_seq cursor")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--direction", choices=["all", "in", "out"], default="all")
    profiles = sub.add_parser("profiles", help="List cached sender profiles for one observed conversation")
    profiles.add_argument("--scene", choices=["c2c", "group", "guild", "dm"], required=True)
    profiles.add_argument("--chat-id", required=True)
    profiles.add_argument("--user-id")
    add_page_arguments(profiles)
    profile = sub.add_parser("profile", help="Show one cached sender profile")
    profile.add_argument("--scene", choices=["c2c", "group", "guild", "dm"], required=True)
    profile.add_argument("--chat-id", required=True)
    profile.add_argument("--user-id", required=True)
    ack = sub.add_parser("ack", help="Mark a bridge message processed")
    ack.add_argument("--seq", type=int, required=True)
    upload = sub.add_parser("upload", help="Upload file bytes only; does NOT send to QQ")
    upload.add_argument("--file", required=True)
    send = sub.add_parser("send", help="Send an authorized QQ reply or proactive message")
    send.add_argument("--key", required=True, help="Stable idempotency key; reuse on retry")
    send.add_argument("--reply-to", type=int)
    send.add_argument("--scene", choices=["c2c", "group", "guild", "dm"])
    send.add_argument("--chat-id")
    send.add_argument("--text", default="")
    source = send.add_mutually_exclusive_group()
    source.add_argument("--media-id")
    source.add_argument("--media-url", help="Public HTTPS direct media link; QQ fetches it natively")
    send.add_argument("--filename", help="Optional display filename for media-id or media-url")
    send.add_argument("--kind", choices=["image", "voice", "video", "file"])
    send.add_argument("--markdown", action="store_true")
    result = sub.add_parser("result", help="Look up a send by its idempotency key")
    result.add_argument("--key", required=True)
    return parser


def page_query(args, *, include_user=False):
    if args.offset < 0 or not 1 <= args.limit <= 100:
        raise ValueError("offset >= 0 and limit 1..100 required.")
    query = {"scene": args.scene, "chat_id": args.chat_id, "limit": args.limit, "offset": args.offset}
    if include_user and args.user_id:
        query["user_id"] = args.user_id
    return urllib.parse.urlencode(query)


def valid_media_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("media-url must be a public HTTPS direct link without embedded credentials or fragment.")


def dispatch(args):
    if args.action == "status":
        return request("GET", "/v1/status")
    if args.action == "capabilities":
        return request("GET", "/v1/capabilities")
    if args.action == "sessions":
        return request("GET", "/v1/sessions")
    if args.action == "profiles":
        return request("GET", "/v1/profiles?" + page_query(args, include_user=True))
    if args.action == "profile":
        path = "/v1/profiles/{}/{}/{}".format(*(urllib.parse.quote(v, safe="") for v in (args.scene, args.chat_id, args.user_id)))
        return request("GET", path)
    if args.action == "history":
        if args.after < 0 or not 1 <= args.limit <= 100:
            raise ValueError("after >= 0 and limit 1..100 required.")
        query = urllib.parse.urlencode({"after": args.after, "limit": args.limit, "direction": args.direction})
        path = "/v1/sessions/{}/{}/history?{}".format(
            urllib.parse.quote(args.scene, safe=""), urllib.parse.quote(args.chat_id, safe=""), query)
        return request("GET", path)
    if args.action == "inbox":
        if args.after < 0 or not 1 <= args.limit <= 100:
            raise ValueError("after >= 0 and limit 1..100 required.")
        query = {"after": args.after, "limit": args.limit}
        if args.scene:
            query["scene"] = args.scene
        if args.chat_id:
            query["chat_id"] = args.chat_id
        return request("GET", "/v1/messages?" + urllib.parse.urlencode(query))
    if args.action == "ack":
        if args.seq < 1:
            raise ValueError("seq must be >= 1")
        return request("POST", f"/v1/messages/{args.seq}/ack", {})
    if args.action == "upload":
        return upload_file(args.file)
    if args.action == "result":
        return request("GET", "/v1/sends/" + urllib.parse.quote(args.key, safe=""))
    if args.action == "send":
        media_source = args.media_id or args.media_url
        if not args.text.strip() and not media_source:
            raise ValueError("Text, media-id, or media-url required.")
        if bool(media_source) != bool(args.kind):
            raise ValueError("media-id/media-url and kind must be specified together.")
        if args.filename and not media_source:
            raise ValueError("filename requires media-id or media-url.")
        if args.media_url:
            valid_media_url(args.media_url)
        if args.reply_to is None and not (args.scene and args.chat_id):
            raise ValueError("Proactive send needs scene and chat-id; reply needs reply-to.")
        if args.reply_to is not None and (args.scene or args.chat_id):
            raise ValueError("Replies derive routing from reply-to; do not specify scene/chat-id.")
        data = {"idempotency_key": args.key, "text": args.text, "markdown": args.markdown}
        if args.reply_to is not None:
            data["reply_to"] = args.reply_to
        else:
            data.update(scene=args.scene, chat_id=args.chat_id)
        if args.media_id:
            data.update(media_id=args.media_id, media_kind=args.kind)
        elif args.media_url:
            data.update(media_url=args.media_url, media_kind=args.kind)
        if args.filename:
            data["filename"] = args.filename
        return request("POST", "/v1/messages/send", data)
    raise ValueError("Unsupported action")


def main():
    args = make_parser().parse_args()
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
