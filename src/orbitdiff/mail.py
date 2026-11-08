"""The one way email leaves Orbit Diff.

Development and tests send to Mailpit (docker-compose.yml), which keeps every message and
delivers none. A deployment points the `ORBITDIFF_SMTP_*` settings at its email provider,
over TLS and with its credentials. Every transactional provider speaks SMTP, so changing
provider changes settings, not code.
"""

import smtplib
import ssl
from email.message import EmailMessage

from orbitdiff.config import settings

SMTP_TIMEOUT_S = 30


def send(message: EmailMessage) -> None:
    cfg = settings()
    if cfg.smtp_username and not cfg.smtp_starttls:
        raise RuntimeError("refusing to send SMTP credentials without ORBITDIFF_SMTP_STARTTLS")
    if "From" not in message:
        message["From"] = cfg.mail_from
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT_S) as smtp:
        if cfg.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if cfg.smtp_username:
            password = cfg.smtp_password.get_secret_value() if cfg.smtp_password else ""
            smtp.login(cfg.smtp_username, password)
        smtp.send_message(message)
