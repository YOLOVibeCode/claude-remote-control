"""Prints instead of sending: dry runs and tests."""
from __future__ import annotations


class ConsoleText:
    name = "console"
    secrets = ()

    def send_text(self, to: str, body: str) -> None:
        print(f"[text to {to}] {body}")


class ConsoleEmail:
    name = "console"
    secrets = ()

    def send_email(self, to: str, subject: str, html: str) -> None:
        print(f"[email to {to}] {subject} ({len(html)} bytes of HTML)")
