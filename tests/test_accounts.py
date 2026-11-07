"""Signed links and email addresses: what a link in an email can and can't be made to do."""

from datetime import timedelta

import pytest
from pydantic import SecretStr

from orbitdiff import accounts
from orbitdiff.config import settings


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("ORBITDIFF_SIGNING_KEY", "test-only-signing-key")
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_a_signed_token_round_trips():
    token = accounts.sign("feedback", {"e": 7, "u": 3}, timedelta(days=1))
    assert accounts.verify("feedback", token) == {"e": 7, "u": 3}


def test_a_token_is_good_for_one_purpose_only():
    token = accounts.sign("feedback", {"e": 7, "u": 3}, timedelta(days=1))
    assert accounts.verify("login", token) is None


def test_an_altered_or_expired_token_is_refused():
    token = accounts.sign("feedback", {"e": 7, "u": 3}, timedelta(days=1))
    body, mac = token.split(".")
    forged = accounts.sign("feedback", {"e": 8, "u": 3}, timedelta(days=1)).split(".")[0]
    assert accounts.verify("feedback", f"{forged}.{mac}") is None
    assert accounts.verify("feedback", f"{body}.{mac[:-2]}") is None
    assert accounts.verify("feedback", "not-a-token") is None
    expired = accounts.sign("feedback", {"e": 7, "u": 3}, timedelta(seconds=-1))
    assert accounts.verify("feedback", expired) is None


def test_a_token_signed_with_another_key_is_refused(monkeypatch):
    token = accounts.sign("feedback", {"e": 7, "u": 3}, timedelta(days=1))
    monkeypatch.setenv("ORBITDIFF_SIGNING_KEY", "another-test-only-key")
    settings.cache_clear()
    assert accounts.verify("feedback", token) is None


@pytest.mark.parametrize("key", [None, "", "short"])
def test_signing_without_a_real_key_fails_loudly(monkeypatch, key):
    monkeypatch.setattr(settings(), "signing_key", None if key is None else SecretStr(key))
    with pytest.raises(RuntimeError, match="ORBITDIFF_SIGNING_KEY"):
        accounts.sign("feedback", {}, timedelta(days=1))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Ana@Example.COM ", "ana@example.com"),
        ("ana@localhost", None),
        ("ana example@x.com", None),
        ("@example.com", None),
        ("ana", None),
    ],
)
def test_normalize_email(raw, expected):
    assert accounts.normalize_email(raw) == expected
