"""Any SMTP mailbox (Gmail app password, Fastmail, M365, …) via the standard library."""
from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage

from ..ports import DeliveryError


class SmtpEmail:
    name = "smtp"
    secrets = ("username", "password")

    def __init__(self, username: str, password: str, host: str, from_: str, port: int = 587, starttls: bool = True):
        self._user, self._pass, self._host, self._from = username, password, host, from_
        self._port, self._tls = int(port), bool(starttls)

    def send_email(self, to: str, subject: str, html: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._from, to, subject
        msg.set_content("This report is HTML; open it in a mail client that shows HTML.")
        msg.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self._host, self._port, timeout=15) as s:
                s.ehlo()
                if self._tls:
                    s.starttls()
                    s.ehlo()
                s.login(self._user, self._pass)
                s.send_message(msg)
        except (smtplib.SMTPException, OSError, socket.timeout) as e:
            raise DeliveryError(self.name, f"failed ({type(e).__name__})") from None
