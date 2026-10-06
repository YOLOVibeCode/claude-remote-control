"""Contract suite: every TextSender and EmailSender provider, against local stub servers.

A new provider joins by adding one case to TEXT_CASES or EMAIL_CASES.
"""
from __future__ import annotations

import base64
import json
import os
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.notify import ConfigError, build_email_sender, build_heartbeat_sender, build_text_sender  # noqa: E402
from claude_rc.ports import DeliveryError  # noqa: E402

SECRET = "sk_live_DO_NOT_LEAK_123456"


class StubHTTP:
    """Records requests; answers with the configured status."""

    def __init__(self):
        self.requests = []
        self.status = 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": self.rfile.read(n).decode()})
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}' if outer.status < 300 else b'{"error":"nope"}')

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StubSMTP:
    """Just enough SMTP (no TLS) to capture one message."""

    def __init__(self):
        self.messages = []
        self.auth = []
        outer = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda s: self.wfile.write((s + "\r\n").encode())  # noqa: E731
                w("220 stub")
                mail = {"rcpt": [], "data": ""}
                while True:
                    line = self.rfile.readline().decode().rstrip("\r\n")
                    if not line:
                        return
                    cmd = line.split(" ", 1)[0].upper()
                    if cmd in ("EHLO", "HELO"):
                        w("250-stub"); w("250 AUTH PLAIN LOGIN")  # noqa: E702
                    elif cmd == "AUTH":
                        outer.auth.append(line); w("235 ok")  # noqa: E702
                    elif cmd == "MAIL":
                        mail["from"] = line; w("250 ok")  # noqa: E702
                    elif cmd == "RCPT":
                        mail["rcpt"].append(line); w("250 ok")  # noqa: E702
                    elif cmd == "DATA":
                        w("354 go")
                        buf = []
                        while True:
                            l2 = self.rfile.readline().decode()
                            if l2.rstrip("\r\n") == ".":
                                break
                            buf.append(l2)
                        mail["data"] = "".join(buf)
                        outer.messages.append(mail)
                        w("250 queued")
                    elif cmd == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# --- cases ---------------------------------------------------------------------------------
# Each case: provider config builder (given the stub url), env, and a check of the recorded request.

def relay_text(url):
    return {"provider": "noctusoft-relay", "env": {"api_key": "RELAY_KEY"}, "options": {"base_url": url}}


def twilio_text(url):
    return {"provider": "twilio", "env": {"account_sid": "TW_SID", "auth_token": "TW_TOKEN", "from": "TW_FROM"},
            "options": {"base_url": url}}


def check_relay_text(t, req):
    t.assertEqual(req["path"], "/sms/send")
    t.assertEqual(req["headers"]["authorization"], f"Bearer {SECRET}")
    t.assertEqual(req["headers"]["x-app-env"], "production")
    t.assertEqual(json.loads(req["body"]), {"to": "+15125550100", "body": "claude-rc: restarted a"})


def check_twilio_text(t, req):
    t.assertEqual(req["path"], "/2010-04-01/Accounts/AC123/Messages.json")
    want = base64.b64encode(f"AC123:{SECRET}".encode()).decode()
    t.assertEqual(req["headers"]["authorization"], f"Basic {want}")
    form = parse_qs(req["body"])
    t.assertEqual(form, {"To": ["+15125550100"], "From": ["+15125550199"], "Body": ["claude-rc: restarted a"]})


TEXT_CASES = [
    ("noctusoft-relay", relay_text, {"RELAY_KEY": SECRET}, check_relay_text),
    ("twilio", twilio_text, {"TW_SID": "AC123", "TW_TOKEN": SECRET, "TW_FROM": "+15125550199"}, check_twilio_text),
]


def relay_email(url):
    return {"provider": "noctusoft-relay", "env": {"api_key": "RELAY_KEY"}, "options": {"base_url": url}}


def sendgrid_email(url):
    return {"provider": "sendgrid", "env": {"api_key": "SG_KEY"}, "options": {"base_url": url, "from": "rc@example.com"}}


def check_relay_email(t, req):
    t.assertEqual(req["path"], "/email/send")
    t.assertEqual(req["headers"]["authorization"], f"Bearer {SECRET}")
    t.assertEqual(req["headers"]["x-app-env"], "production")
    t.assertEqual(json.loads(req["body"]), {"to": "me@example.com", "subject": "claude-rc: 2/2 OK", "html": "<p>ok</p>"})


def check_sendgrid_email(t, req):
    t.assertEqual(req["path"], "/v3/mail/send")
    t.assertEqual(req["headers"]["authorization"], f"Bearer {SECRET}")
    t.assertEqual(json.loads(req["body"]), {
        "personalizations": [{"to": [{"email": "me@example.com"}]}],
        "from": {"email": "rc@example.com"},
        "subject": "claude-rc: 2/2 OK",
        "content": [{"type": "text/html", "value": "<p>ok</p>"}],
    })


EMAIL_CASES = [
    ("noctusoft-relay", relay_email, {"RELAY_KEY": SECRET}, check_relay_email),
    ("sendgrid", sendgrid_email, {"SG_KEY": SECRET}, check_sendgrid_email),
]


class TextContract(unittest.TestCase):
    def setUp(self):
        self.stub = StubHTTP()

    def tearDown(self):
        self.stub.close()

    def test_each_provider_sends_its_vendors_request(self):
        for name, cfg, env, check in TEXT_CASES:
            with self.subTest(provider=name):
                self.stub.requests.clear()
                build_text_sender(cfg(self.stub.url), env).send_text("+15125550100", "claude-rc: restarted a")
                self.assertEqual(len(self.stub.requests), 1)
                check(self, self.stub.requests[0])

    def test_each_provider_raises_delivery_error_without_leaking_secrets(self):
        self.stub.status = 500
        for name, cfg, env, _ in TEXT_CASES:
            with self.subTest(provider=name):
                with self.assertRaises(DeliveryError) as ctx:
                    build_text_sender(cfg(self.stub.url), env).send_text("+15125550100", "x")
                self.assertEqual(ctx.exception.status, 500)
                self.assertNotIn(SECRET, str(ctx.exception))

    def test_unreachable_is_a_delivery_error(self):
        for name, cfg, env, _ in TEXT_CASES:
            with self.subTest(provider=name):
                with self.assertRaises(DeliveryError):
                    build_text_sender(cfg("http://127.0.0.1:9"), env).send_text("+1", "x")

    def test_missing_secret_names_the_variable_not_a_value(self):
        for name, cfg, env, _ in TEXT_CASES:
            with self.subTest(provider=name):
                with self.assertRaises(ConfigError) as ctx:
                    build_text_sender(cfg(self.stub.url), {})
                self.assertIn(next(iter(cfg("").get("env").values())), str(ctx.exception))


class EmailContract(unittest.TestCase):
    def setUp(self):
        self.stub = StubHTTP()

    def tearDown(self):
        self.stub.close()

    def test_each_http_provider_sends_its_vendors_request(self):
        for name, cfg, env, check in EMAIL_CASES:
            with self.subTest(provider=name):
                self.stub.requests.clear()
                build_email_sender(cfg(self.stub.url), env).send_email("me@example.com", "claude-rc: 2/2 OK", "<p>ok</p>")
                self.assertEqual(len(self.stub.requests), 1)
                check(self, self.stub.requests[0])

    def test_each_http_provider_raises_delivery_error_without_leaking_secrets(self):
        self.stub.status = 401
        for name, cfg, env, _ in EMAIL_CASES:
            with self.subTest(provider=name):
                with self.assertRaises(DeliveryError) as ctx:
                    build_email_sender(cfg(self.stub.url), env).send_email("me@example.com", "s", "<p>x</p>")
                self.assertNotIn(SECRET, str(ctx.exception))

    def test_smtp_provider_delivers_an_html_message(self):
        smtp = StubSMTP()
        try:
            cfg = {"provider": "smtp", "env": {"username": "SMTP_USER", "password": "SMTP_PASS"},
                   "options": {"host": "127.0.0.1", "port": smtp.port, "from": "rc@example.com", "starttls": False}}
            build_email_sender(cfg, {"SMTP_USER": "rc", "SMTP_PASS": SECRET}).send_email(
                "me@example.com", "claude-rc: 2/2 OK", "<p>ok</p>")
            self.assertEqual(len(smtp.messages), 1)
            m = smtp.messages[0]
            self.assertIn("me@example.com", m["rcpt"][0])
            self.assertIn("Subject: claude-rc: 2/2 OK", m["data"])
            self.assertIn("text/html", m["data"])
            self.assertTrue(smtp.auth)
        finally:
            smtp.close()

    def test_smtp_failure_is_a_delivery_error_without_the_password(self):
        cfg = {"provider": "smtp", "env": {"username": "SMTP_USER", "password": "SMTP_PASS"},
               "options": {"host": "127.0.0.1", "port": 9, "from": "rc@example.com", "starttls": False}}
        with self.assertRaises(DeliveryError) as ctx:
            build_email_sender(cfg, {"SMTP_USER": "rc", "SMTP_PASS": SECRET}).send_email("me@example.com", "s", "x")
        self.assertNotIn(SECRET, str(ctx.exception))


class HeartbeatContract(unittest.TestCase):
    def setUp(self):
        self.stub = StubHTTP()

    def tearDown(self):
        self.stub.close()

    def cfg(self, url):
        return {"provider": "http", "env": {"token": "HB_TOKEN"}, "options": {"url": f"{url}/v1/heartbeat"}}

    def test_http_beat_posts_json_with_its_own_bearer_token(self):
        build_heartbeat_sender(self.cfg(self.stub.url), {"HB_TOKEN": SECRET}).beat(
            {"source": "claude-rc", "host": "mac", "ok": 16, "total": 16, "needs_you": 0})
        req = self.stub.requests[0]
        self.assertEqual(req["path"], "/v1/heartbeat")
        self.assertEqual(req["headers"]["authorization"], f"Bearer {SECRET}")
        self.assertEqual(json.loads(req["body"]), {"source": "claude-rc", "host": "mac", "ok": 16, "total": 16, "needs_you": 0})

    def test_http_beat_failure_is_a_delivery_error_without_the_token(self):
        self.stub.status = 401
        with self.assertRaises(DeliveryError) as ctx:
            build_heartbeat_sender(self.cfg(self.stub.url), {"HB_TOKEN": SECRET}).beat({"source": "claude-rc"})
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_console_beat_needs_nothing(self):
        build_heartbeat_sender({"provider": "console"}, {}).beat({"source": "claude-rc"})


class RegistryTest(unittest.TestCase):
    def test_unknown_provider_lists_the_known_ones(self):
        with self.assertRaises(ConfigError) as ctx:
            build_text_sender({"provider": "carrier-pigeon"}, {})
        self.assertIn("twilio", str(ctx.exception))

    def test_a_text_only_provider_cannot_be_used_for_email(self):
        with self.assertRaises(ConfigError):
            build_email_sender({"provider": "twilio"}, {})

    def test_console_provider_needs_nothing(self):
        build_text_sender({"provider": "console"}, {}).send_text("+1", "hello")
        build_email_sender({"provider": "console"}, {}).send_email("a@b", "s", "<p>x</p>")


if __name__ == "__main__":
    unittest.main()
