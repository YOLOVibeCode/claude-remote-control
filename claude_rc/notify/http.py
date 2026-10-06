"""Tiny POST helper shared by HTTP providers. Errors never carry request headers or bodies."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from typing import Dict

from ..ports import DeliveryError


def post(provider: str, url: str, headers: Dict[str, str], body: bytes, timeout: float = 15) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise DeliveryError(provider, "rejected", resp.status)
    except urllib.error.HTTPError as e:
        raise DeliveryError(provider, "rejected", e.code) from None
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise DeliveryError(provider, f"unreachable ({type(reason).__name__})") from None
