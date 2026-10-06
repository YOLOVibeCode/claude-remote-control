"""The seams. Each consumer depends only on the narrow interface it uses (ISP)."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover - python < 3.8
    Protocol = object  # type: ignore

from .core import Entry, History, Record


class SessionRegistry(Protocol):
    def live_sessions(self) -> List[Record]: ...


class Multiplexer(Protocol):
    def has_session(self, name: str) -> bool: ...
    def new_session(self, name: str, directory: str, argv: Sequence[str]) -> None: ...
    def send_keys(self, name: str, text: str) -> None: ...
    def capture(self, name: str, lines: int, ansi: bool = False) -> str: ...


class ManifestStore(Protocol):
    def load(self) -> List[Entry]: ...
    def save(self, entries: List[Entry]) -> None: ...


class StateStore(Protocol):
    def load(self) -> Tuple[Dict[str, str], Dict[str, History], List[str]]: ...
    def save(self, conditions: Dict[str, str], histories: Dict[str, History], queued: List[str]) -> None: ...


class TrustSource(Protocol):
    def trusted(self, directory: str) -> bool: ...


class Clock(Protocol):
    def now(self) -> float: ...


class TextSender(Protocol):
    def send_text(self, to: str, body: str) -> None: ...


class EmailSender(Protocol):
    def send_email(self, to: str, subject: str, html: str) -> None: ...


class HeartbeatSender(Protocol):
    def beat(self, payload: dict) -> None: ...


class DeliveryError(Exception):
    """A provider could not deliver. Carries no secrets."""

    def __init__(self, provider: str, detail: str, status: Optional[int] = None):
        super().__init__(f"{provider}: {detail}" + (f" (HTTP {status})" if status else ""))
        self.provider, self.detail, self.status = provider, detail, status
