"""`orbitdiff doctor`: every check has its say, and nothing is sent unless asked."""

import pytest

from orbitdiff import cli, doctor, mail


def broken() -> str:
    raise ConnectionRefusedError("nothing listens on 6379\nand a traceback nobody needs")


@pytest.fixture
def checks(monkeypatch):
    def use(*checks: doctor.Check) -> None:
        monkeypatch.setattr(doctor, "CHECKS", checks)

    return use


def test_a_failing_check_is_named_with_where_to_look_and_the_rest_still_run(checks, capsys):
    checks(
        doctor.Check("queue", broken, "ORBITDIFF_REDIS_URL"),
        doctor.Check("database", lambda: "PostGIS 3.7.0, schema 0007", "ORBITDIFF_DATABASE_URL"),
    )
    assert doctor.run() is False
    assert capsys.readouterr().out.splitlines() == [
        "FAIL  queue     ConnectionRefusedError: nothing listens on 6379",
        "                look at: ORBITDIFF_REDIS_URL",
        "ok    database  PostGIS 3.7.0, schema 0007",
    ]


def test_the_command_fails_when_a_check_does(checks):
    checks(doctor.Check("queue", broken, "ORBITDIFF_REDIS_URL"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["doctor"])
    assert stopped.value.code == 1
    checks(doctor.Check("queue", lambda: "Redis answers", "ORBITDIFF_REDIS_URL"))
    cli.main(["doctor"])  # healthy: returns


def test_an_email_is_sent_only_when_an_address_is_given(checks, monkeypatch, capsys):
    sent: list = []
    monkeypatch.setattr(mail, "send", sent.append)
    checks(doctor.Check("queue", lambda: "Redis answers", "ORBITDIFF_REDIS_URL"))

    assert doctor.run() is True and sent == []
    assert doctor.run("me@example.com") is True
    (message,) = sent
    assert message["To"] == "me@example.com" and "test email" in message.get_content()
    assert "sent to me@example.com" in capsys.readouterr().out


def test_a_setup_with_no_signing_key_or_admins_is_not_healthy(monkeypatch):
    from orbitdiff.config import settings

    monkeypatch.setenv("ORBITDIFF_SIGNING_KEY", "")
    monkeypatch.setenv("ORBITDIFF_ADMIN_EMAILS", "[]")
    settings.cache_clear()
    try:
        by_name = {c.name: c for c in doctor.CHECKS}
        with pytest.raises(RuntimeError, match="ORBITDIFF_SIGNING_KEY"):
            by_name["signing key"].run()
        with pytest.raises(RuntimeError, match="no admin addresses"):
            by_name["accounts"].run()
    finally:
        settings.cache_clear()
