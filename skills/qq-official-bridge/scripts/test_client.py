"""Offline-only CLI contract tests; never contacts QQ or a public service."""
import argparse
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("qq_bridge_client", Path(__file__).with_name("qq_bridge.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ClientTests(unittest.TestCase):
    def test_missing_credentials(self):
        with patch.dict(c.os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                c.connection()

    def test_block_remote_http(self):
        with patch.dict(c.os.environ, {"QQ_BRIDGE_URL": "http://example.com", "QQ_BRIDGE_TOKEN": "fake"}, clear=True):
            with self.assertRaises(ValueError):
                c.connection()

    def test_mcp_is_wrong_endpoint(self):
        with patch.dict(c.os.environ, {"QQ_BRIDGE_URL": "https://example.com/mcp", "QQ_BRIDGE_TOKEN": "fake"}, clear=True):
            with self.assertRaises(ValueError):
                c.connection()

    def test_inbox_does_not_send(self):
        args = c.make_parser().parse_args(["inbox", "--after", "7", "--limit", "10"])
        with patch.object(c, "request", return_value={"items": []}) as req:
            c.dispatch(args)
        self.assertEqual(req.call_args.args, ("GET", "/v1/messages?after=7&limit=10"))

    def test_stable_send_key(self):
        args = c.make_parser().parse_args(["send", "--reply-to", "123", "--text", "hello", "--key", "reply-123"])
        with patch.object(c, "request", return_value={"status": "queued"}) as req:
            c.dispatch(args)
        self.assertEqual(req.call_args.args[2]["idempotency_key"], "reply-123")
        self.assertNotIn("scene", req.call_args.args[2])

    def test_proactive_requires_route(self):
        args = c.make_parser().parse_args(["send", "--text", "hello", "--key", "x"])
        with self.assertRaises(ValueError):
            c.dispatch(args)

    def test_upload_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            path.write_bytes(b"fake image content")
            with patch.object(c, "request", return_value={"media_id": "fake"}) as req:
                c.upload_file(str(path))
            self.assertEqual(req.call_args.args, ("POST", "/v1/media"))
            self.assertIn(b'name="file"', req.call_args.kwargs["raw"])
            self.assertIn(b'filename="upload.png"', req.call_args.kwargs["raw"])

    def test_media_url_send_contract(self):
        args = c.make_parser().parse_args([
            "send", "--reply-to", "123", "--media-url", "https://media.example/picture.png",
            "--kind", "image", "--filename", "picture.png", "--key", "url-123",
        ])
        with patch.object(c, "request", return_value={"status": "queued"}) as req:
            c.dispatch(args)
        body = req.call_args.args[2]
        self.assertEqual(body["media_url"], "https://media.example/picture.png")
        self.assertEqual(body["filename"], "picture.png")
        self.assertNotIn("media_id", body)

    def test_media_url_requires_https(self):
        args = c.make_parser().parse_args([
            "send", "--reply-to", "123", "--media-url", "http://media.example/a.png",
            "--kind", "image", "--key", "url-123",
        ])
        with self.assertRaises(ValueError):
            c.dispatch(args)

    def test_capabilities_profiles_and_history_routes(self):
        cases = [
            (["capabilities"], "/v1/capabilities"),
            (["profiles", "--scene", "c2c", "--chat-id", "chat", "--user-id", "user", "--limit", "7", "--offset", "2"],
             "/v1/profiles?scene=c2c&chat_id=chat&limit=7&offset=2&user_id=user"),
            (["profile", "--scene", "c2c", "--chat-id", "chat/id", "--user-id", "user id"],
             "/v1/profiles/c2c/chat%2Fid/user%20id"),
            (["history", "--scene", "group", "--chat-id", "chat/id", "--after", "9", "--limit", "7", "--direction", "out"],
             "/v1/sessions/group/chat%2Fid/history?after=9&limit=7&direction=out"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv), patch.object(c, "request", return_value={}) as req:
                c.dispatch(c.make_parser().parse_args(argv))
                self.assertEqual(req.call_args.args, ("GET", expected))

    def test_redirect_never_forward_token(self):
        self.assertIsNone(c.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))


if __name__ == "__main__":
    unittest.main()
