"""Heartbeat providers: tell something outside this Mac that the watchdog ran.

The outside service (cloud-agents' /v1/heartbeat) texts when beats stop, which covers what
nothing on the Mac can report: sleep, power loss, the FileVault screen, no network.
"""
from __future__ import annotations

import json

from .http import post


class HttpHeartbeat:
    name = "http"
    secrets = ("token",)

    def __init__(self, token: str, url: str):
        self._token, self._url = token, url

    def beat(self, payload: dict) -> None:
        post(self.name, self._url, {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }, json.dumps(payload).encode())


class ConsoleHeartbeat:
    name = "console"
    secrets = ()

    def beat(self, payload: dict) -> None:
        print(f"[heartbeat] {json.dumps(payload)}")
