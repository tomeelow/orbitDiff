"""How email leaves: Mailpit by default, a provider over TLS when configured."""

from email.message import EmailMessage

import pytest

from orbitdiff import mail
from orbitdiff.config import settings


class FakeSmtp:
    """Records what a connection did, in order."""

    connections: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout):
        self.where = (host, port)
        self.calls: list[str] = []
        FakeSmtp.connections.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(f"login {username} {password}")

    def send_message(self, message):
        self.calls.append(f"send from {message['From']}")


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    FakeSmtp.connections = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSmtp)
    for name in ("HOST", "PORT", "STARTTLS", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"ORBITDIFF_SMTP_{name}", raising=False)
    monkeypatch.setenv("ORBITDIFF_MAIL_FROM", "Orbit Diff <a@example.com>")
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_by_default_mail_goes_to_mailpit_with_no_credentials():
    mail.send(EmailMessage())
    connection = FakeSmtp.connections[0]
    assert connection.where == ("localhost", 1025)
    assert connection.calls == ["send from Orbit Diff <a@example.com>"]


def test_a_provider_gets_tls_first_then_credentials(monkeypatch):
    monkeypatch.setenv("ORBITDIFF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ORBITDIFF_SMTP_PORT", "587")
    monkeypatch.setenv("ORBITDIFF_SMTP_STARTTLS", "true")
    monkeypatch.setenv("ORBITDIFF_SMTP_USERNAME", "key")
    monkeypatch.setenv("ORBITDIFF_SMTP_PASSWORD", "not-a-real-secret")
    settings.cache_clear()
    mail.send(EmailMessage())
    connection = FakeSmtp.connections[0]
    assert connection.where == ("smtp.example.com", 587)
    assert connection.calls == [
        "starttls",
        "login key not-a-real-secret",
        "send from Orbit Diff <a@example.com>",
    ]


def test_credentials_never_travel_without_tls(monkeypatch):
    monkeypatch.setenv("ORBITDIFF_SMTP_USERNAME", "key")
    monkeypatch.setenv("ORBITDIFF_SMTP_PASSWORD", "not-a-real-secret")
    settings.cache_clear()
    with pytest.raises(RuntimeError, match="STARTTLS"):
        mail.send(EmailMessage())
    assert FakeSmtp.connections == []
