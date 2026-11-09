"""`orbitdiff doctor`: is this installation set up to work?

Each check asks one question of the configuration as the services see it, and says where
to look when the answer is no. It changes nothing and sends nothing, unless it is given an
address to send one test email to.
"""

import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage

from alembic.config import Config
from alembic.script import ScriptDirectory
from redis import Redis
from sqlalchemy import text

from orbitdiff import accounts, db, mail
from orbitdiff.config import REPO_ROOT, pipeline_config, settings

TIMEOUT_S = 15


def _signing_key() -> str:
    accounts.sign("doctor", {}, timedelta(seconds=1))
    return "set"


def _database() -> str:
    head = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini"))).get_current_head()
    with db.engine().connect() as c:
        postgis = c.execute(text("SELECT postgis_lib_version()")).scalar_one()
        at = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if at != head:
        raise RuntimeError(f"schema is at {at}, the code expects {head}: run the migrations")
    return f"PostGIS {postgis}, schema {at}"


def _queue() -> str:
    Redis.from_url(settings().redis_url, socket_timeout=TIMEOUT_S).ping()
    return "Redis answers"


def _data_dir() -> str:
    folder = settings().data_dir
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder):
        pass
    return f"{folder} is writable"


def _get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=TIMEOUT_S):
        pass


def _tiles() -> str:
    _get(f"{settings().titiler_url}/healthz")
    return "TiTiler answers"


def _catalog() -> str:
    primary = pipeline_config().catalog.primary
    _get(primary.url)
    return f"{primary.name} answers"


def _email() -> str:
    cfg = settings()
    mail.check()
    return f"{cfg.smtp_host}:{cfg.smtp_port} accepts the sign-in, sending as {cfg.mail_from}"


def _admins() -> str:
    count = len(settings().admin_emails)
    if not count:
        raise RuntimeError("no admin addresses: nobody can open #/admin")
    invited = len(settings().invited_emails)
    who = f"and {invited} invited" if invited else "and anyone with an email address"
    return f"{count} admin, {who}, may sign in"


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[], str]  # what it found; raises with the reason when something is wrong
    look_at: str  # the settings behind it


CHECKS = (
    Check("signing key", _signing_key, "ORBITDIFF_SIGNING_KEY"),
    Check("database", _database, "ORBITDIFF_DATABASE_URL, `alembic upgrade head`"),
    Check("queue", _queue, "ORBITDIFF_REDIS_URL"),
    Check("data folder", _data_dir, "ORBITDIFF_DATA_DIR and who owns the folder"),
    Check("map tiles", _tiles, "ORBITDIFF_TITILER_URL"),
    Check("imagery catalog", _catalog, "outbound HTTPS from this machine"),
    Check("email", _email, "ORBITDIFF_SMTP_* and ORBITDIFF_MAIL_FROM"),
    Check("accounts", _admins, "ORBITDIFF_ADMIN_EMAILS"),
)


def trial_email(to: str) -> EmailMessage:
    cfg = settings()
    msg = EmailMessage()
    msg["Subject"] = "Orbit Diff can send email"
    msg["To"] = to
    msg.set_content(
        f"This is the test email `orbitdiff doctor --email` sends.\n\n"
        f"It left through {cfg.smtp_host} as {cfg.mail_from}. Sign-in links and alerts take "
        f"the same road, and their links will point at {cfg.web_url}."
    )
    return msg


def run(email_to: str | None = None) -> bool:
    """Print one line per check; True when every one passed."""
    checks = list(CHECKS)
    if email_to:

        def send() -> str:
            mail.send(trial_email(email_to))
            return f"sent to {email_to}: check that it arrived, and not in spam"

        checks.append(Check("test email", send, "ORBITDIFF_SMTP_* and ORBITDIFF_MAIL_FROM"))
    width = max(len(c.name) for c in checks)
    healthy = True
    for check in checks:
        try:
            print(f"ok    {check.name:<{width}}  {check.run()}")
        except Exception as exc:  # every check gets its say, whatever the others did
            healthy = False
            reason = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            print(f"FAIL  {check.name:<{width}}  {type(exc).__name__}: {reason}")
            print(f"      {'':<{width}}  look at: {check.look_at}")
    return healthy
