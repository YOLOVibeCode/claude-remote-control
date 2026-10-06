"""Noctusoft relay: /sms/send and /email/send with a product key (nsk_…)."""
from __future__ import annotations

import json

from .http import post

TEXT_BASE = "https://api.twilio.noctusoft.com"
MAIL_BASE = "https://api.sendgrid.noctusoft.com"


class _Relay:
    name = "noctusoft-relay"

    def __init__(self, api_key: str, base_url: str, app_env: str = "production"):
        self._key, self._base, self._env = api_key, base_url.rstrip("/"), app_env

    def _post(self, path: str, payload: dict) -> None:
        post(self.name, f"{self._base}{path}", {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "X-App-Env": self._env,
        }, json.dumps(payload).encode())


class RelayText(_Relay):
    secrets = ("api_key",)

    def __init__(self, api_key: str, base_url: str = TEXT_BASE, app_env: str = "production"):
        super().__init__(api_key, base_url, app_env)

    def send_text(self, to: str, body: str) -> None:
        self._post("/sms/send", {"to": to, "body": body})


class RelayEmail(_Relay):
    secrets = ("api_key",)

    def __init__(self, api_key: str, base_url: str = MAIL_BASE, app_env: str = "production"):
        super().__init__(api_key, base_url, app_env)

    def send_email(self, to: str, subject: str, html: str) -> None:
        self._post("/email/send", {"to": to, "subject": subject, "html": html})
