"""Twilio Messages API, direct: form POST with Basic auth (Account SID : Auth Token)."""
from __future__ import annotations

import base64
from urllib.parse import quote, urlencode

from .http import post


class TwilioText:
    name = "twilio"
    secrets = ("account_sid", "auth_token", "from")

    def __init__(self, account_sid: str, auth_token: str, from_: str, base_url: str = "https://api.twilio.com"):
        self._sid, self._token, self._from, self._base = account_sid, auth_token, from_, base_url.rstrip("/")

    def send_text(self, to: str, body: str) -> None:
        auth = base64.b64encode(f"{self._sid}:{self._token}".encode()).decode()
        post(self.name, f"{self._base}/2010-04-01/Accounts/{quote(self._sid)}/Messages.json", {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        }, urlencode({"To": to, "From": self._from, "Body": body}).encode())
