"""Accounts: sign-in by emailed link, sessions, and signed links that need no session.

There are no passwords. A sign-in link proves the address, which alerts need anyway.
Tokens are random and only their hashes are stored, so a database dump signs no one in.
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from redis import Redis
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orbitdiff import db
from orbitdiff.config import AccountsConfig, settings

COOKIE = "orbitdiff_session"
MAX_EMAIL = 254
MIN_KEY_CHARS = 16


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str | None:
    """The address in one canonical form, or None when it can't be one. The sign-in link
    is the real check; this only keeps obvious junk out."""
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > MAX_EMAIL or any(c.isspace() for c in email):
        return None
    return email


def _within_limit(redis: Redis, key: str, limit: int, window_s: int) -> bool:
    """Count one more use of `key`; False once it has been used more than `limit` times in
    the window, which starts at its first use. One transaction, so a key always expires."""
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_s, nx=True)
    count, _ = pipe.execute()
    return count <= limit


def login_allowed(redis: Redis, email: str, client_ip: str, cfg: AccountsConfig) -> bool:
    """Whether a sign-in link may be sent now, so the form can't be used to flood someone's
    inbox or burn through the email provider. The same answer whether or not the address
    has an account. Addresses are hashed: Redis holds no personal data."""
    window_s = cfg.login_limit_minutes * 60
    ip = _within_limit(redis, f"login:ip:{_hash(client_ip)}", cfg.login_per_ip, window_s)
    to = _within_limit(redis, f"login:email:{_hash(email)}", cfg.login_per_email, window_s)
    return ip and to


def new_login_link(s: Session, email: str, cfg: AccountsConfig) -> str:
    """A token for a sign-in link to `email`. It works any number of times until it expires:
    mail scanners open links before people do, and a single-use link would be spent."""
    now = datetime.now(UTC)
    s.execute(delete(db.LoginLink).where(db.LoginLink.expires_utc < now))
    token = secrets.token_urlsafe(32)
    s.add(
        db.LoginLink(
            token_hash=_hash(token),
            email=email,
            expires_utc=now + timedelta(minutes=cfg.login_link_minutes),
        )
    )
    return token


def login_email(to: str, link: str, cfg: AccountsConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Sign in to Orbit Diff"
    msg["To"] = to
    msg.set_content(
        f"Open this link to sign in to Orbit Diff:\n\n{link}\n\n"
        f"It works for {cfg.login_link_minutes} minutes. If you didn't ask to sign in, "
        "ignore this email: no one can sign in without the link."
    )
    return msg


def use_login_link(s: Session, token: str, cfg: AccountsConfig) -> tuple[db.User, str] | None:
    """The user a sign-in link is for (created on first sign-in) and a new session token,
    or None when the link is unknown or expired."""
    link = s.get(db.LoginLink, _hash(token))
    now = datetime.now(UTC)
    if link is None or link.expires_utc < now:
        return None
    s.execute(insert(db.User).values(email=link.email).on_conflict_do_nothing())
    user = s.scalars(select(db.User).where(db.User.email == link.email)).one()
    session = secrets.token_urlsafe(32)
    s.add(
        db.LoginSession(
            token_hash=_hash(session),
            user_id=user.id,
            expires_utc=now + timedelta(days=cfg.session_days),
        )
    )
    return user, session


def user_for_session(s: Session, token: str) -> db.User | None:
    row = s.get(db.LoginSession, _hash(token))
    if row is None or row.expires_utc < datetime.now(UTC):
        return None
    return s.get(db.User, row.user_id)


def end_session(s: Session, token: str) -> None:
    s.execute(delete(db.LoginSession).where(db.LoginSession.token_hash == _hash(token)))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key() -> bytes:
    key = settings().signing_key
    # Empty (as copied from .env.example) would sign links anyone can forge.
    if key is None or len(key.get_secret_value()) < MIN_KEY_CHARS:
        raise RuntimeError("ORBITDIFF_SIGNING_KEY is missing or too short: see .env.example")
    return key.get_secret_value().encode()


def sign(purpose: str, payload: dict, ttl: timedelta) -> str:
    """A tamper-proof token carrying `payload` (ids only, never personal data) for one
    purpose, valid for `ttl`."""
    expires = int((datetime.now(UTC) + ttl).timestamp())
    body = _b64(json.dumps({"p": purpose, "exp": expires, **payload}).encode())
    mac = hmac.new(_key(), f"{purpose}.{body}".encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(purpose: str, token: str) -> dict | None:
    """The payload of a token signed for `purpose`, or None if it was altered, was signed
    for something else, or has expired."""
    body, _, mac = token.partition(".")
    try:
        expected = hmac.new(_key(), f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        payload = json.loads(_unb64(body))
    except ValueError:  # bad base64 or JSON
        return None
    if payload.pop("p") != purpose or payload.pop("exp") < datetime.now(UTC).timestamp():
        return None
    return payload
