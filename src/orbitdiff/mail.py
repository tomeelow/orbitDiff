"""The one way email leaves Orbit Diff.

Development and tests send to Mailpit (docker-compose.yml), which keeps every message and
delivers none. A deployment points `ORBITDIFF_SMTP_HOST` at its email provider (Phase 11).
"""

import smtplib
from email.message import EmailMessage

from orbitdiff.config import settings

SMTP_TIMEOUT_S = 30


def send(message: EmailMessage) -> None:
    cfg = settings()
    if "From" not in message:
        message["From"] = cfg.mail_from
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT_S) as smtp:
        smtp.send_message(message)
