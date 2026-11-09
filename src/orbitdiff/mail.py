"""The one way email leaves Orbit Diff.

Development and tests send to Mailpit (docker-compose.yml), which keeps every message and
delivers none. A deployment points the `ORBITDIFF_SMTP_*` settings at its email provider,
over TLS and with its credentials. Every transactional provider speaks SMTP, so changing
provider changes settings, not code.
"""

import smtplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import EmailMessage

from orbitdiff.config import settings

SMTP_TIMEOUT_S = 30


@contextmanager
def _signed_in() -> Iterator[smtplib.SMTP]:
    cfg = settings()
    if cfg.smtp_username and not cfg.smtp_starttls:
        raise RuntimeError("refusing to send SMTP credentials without ORBITDIFF_SMTP_STARTTLS")
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT_S) as smtp:
        if cfg.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if cfg.smtp_username:
            password = cfg.smtp_password.get_secret_value() if cfg.smtp_password else ""
            smtp.login(cfg.smtp_username, password)
        yield smtp


def send(message: EmailMessage) -> None:
    if "From" not in message:
        message["From"] = settings().mail_from
    with _signed_in() as smtp:
        smtp.send_message(message)


def check() -> None:
    """Connect and sign in as `send` would, and send nothing (`orbitdiff doctor`)."""
    with _signed_in() as smtp:
        smtp.noop()
