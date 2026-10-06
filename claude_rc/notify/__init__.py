"""Pluggable delivery. A provider is a class registered under a name; config picks one per channel.

Config shape (per channel):
    {"provider": "twilio", "to": "+1…",
     "env": {"account_sid": "TWILIO_ACCOUNT_SID", "auth_token": "TWILIO_AUTH_TOKEN", "from": "TWILIO_FROM"},
     "options": {"base_url": "…"}}
`env` maps each constructor secret to the NAME of an environment variable; values never live in config.
`options` are plain, non-secret keyword arguments.

Adding a vendor: write a class with send_text and/or send_email, then add it below.
"""
from __future__ import annotations

from typing import Dict, Mapping

from ..ports import EmailSender, TextSender
from .console import ConsoleEmail, ConsoleText
from .relay import RelayEmail, RelayText
from .sendgrid import SendGridEmail
from .smtp import SmtpEmail
from .twilio import TwilioText

TEXT_PROVIDERS: Dict[str, type] = {
    "noctusoft-relay": RelayText,
    "twilio": TwilioText,
    "console": ConsoleText,
}

EMAIL_PROVIDERS: Dict[str, type] = {
    "noctusoft-relay": RelayEmail,
    "sendgrid": SendGridEmail,
    "smtp": SmtpEmail,
    "console": ConsoleEmail,
}


class ConfigError(ValueError):
    pass


def _kwarg(name: str) -> str:
    return "from_" if name == "from" else name   # `from` is a keyword in Python


def _build(kind: str, registry: Dict[str, type], cfg: Mapping, env: Mapping[str, str]):
    name = cfg.get("provider", "")
    cls = registry.get(name)
    if cls is None:
        raise ConfigError(f"unknown {kind} provider {name!r}; known: {', '.join(sorted(registry))}")
    names = dict(cfg.get("env", {}))
    kwargs = {}
    for secret in cls.secrets:
        var = names.get(secret)
        if not var:
            raise ConfigError(f"{kind} provider {name}: config env.{secret} must name an environment variable")
        value = env.get(var, "")
        if not value:
            raise ConfigError(f"{kind} provider {name}: environment variable {var} is not set")
        kwargs[_kwarg(secret)] = value
    for key, value in dict(cfg.get("options", {})).items():
        kwargs[_kwarg(key)] = value
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise ConfigError(f"{kind} provider {name}: {e}") from None


def build_text_sender(cfg: Mapping, env: Mapping[str, str]) -> TextSender:
    return _build("text", TEXT_PROVIDERS, cfg, env)


def build_email_sender(cfg: Mapping, env: Mapping[str, str]) -> EmailSender:
    return _build("email", EMAIL_PROVIDERS, cfg, env)
