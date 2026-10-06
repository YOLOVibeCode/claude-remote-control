"""SendGrid v3 mail send, direct."""
from __future__ import annotations

import json

from .http import post


class SendGridEmail:
    name = "sendgrid"
    secrets = ("api_key",)

    def __init__(self, api_key: str, from_: str, base_url: str = "https://api.sendgrid.com"):
        self._key, self._from, self._base = api_key, from_, base_url.rstrip("/")

    def send_email(self, to: str, subject: str, html: str) -> None:
        post(self.name, f"{self._base}/v3/mail/send", {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }, json.dumps({
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": self._from},
            "subject": subject,
            "content": [{"type": "text/html", "value": html}],
        }).encode())
