"""The one alert-rule evaluator, and which email an event needs: every reason an event
doesn't reach a user, and why it reaches them at most twice."""

from datetime import UTC, datetime

import pytest

from orbitdiff import db, notify
from orbitdiff.alerts import AlertRule, default_rule, should_alert, would_alert
from orbitdiff.config import pipeline_config

RULE = AlertRule(min_ha=0.5)


@pytest.mark.parametrize(
    ("status", "category", "area_ha", "rule", "expected"),
    [
        ("pending", "vegetation_loss", 0.6, RULE, (True, "alert")),
        ("confirmed", "solar_industry", 0.5, RULE, (True, "alert")),
        ("confirmed", "water", 0.5, RULE, (False, "category off")),  # opt-in
        ("pending", "vegetation_loss", 0.6, AlertRule(0.5, enabled=False), (False, "alerts off")),
        ("dismissed", "vegetation_loss", 0.6, RULE, (False, "dismissed")),
        ("seasonal", "water", 2.0, RULE, (False, "seasonal")),
        ("seasonal", "vegetation_loss", 2.0, AlertRule(0.5, skip_seasonal=False), (True, "alert")),
        ("pending", "other", 2.0, RULE, (False, "category off")),
        ("pending", "buildings_roads", 0.4, RULE, (False, "under 0.5 ha")),
    ],
)
def test_should_alert_says_why_not(status, category, area_ha, rule, expected):
    assert should_alert(status, category, area_ha, rule) == expected


def test_the_default_rule_comes_from_config():
    assert default_rule(pipeline_config().alerts).min_ha == pipeline_config().alerts.default_min_ha


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["pending"], (True, "alert")),
        (["pending", "confirmed"], (True, "alert")),
        (["pending", "dismissed"], (True, "alert")),  # the pending alert went out first
        (["seasonal"], (False, "seasonal")),  # seasonal from the start: never alerted
        (["pending", "confirmed", "seasonal"], (True, "alert")),
    ],
)
def test_would_alert_counts_an_alert_a_later_pass_retracted(statuses, expected):
    assert would_alert(statuses, "vegetation_loss", 1.0, RULE) == expected


def test_would_alert_says_why_from_the_latest_status():
    assert would_alert(["pending"], "water", 1.0, RULE) == (False, "category off")


SINCE = datetime(2021, 5, 1, tzinfo=UTC)


def event(status: str, at: datetime = datetime(2021, 5, 5, tzinfo=UTC), **kw) -> db.Event:
    fields = {"category": "vegetation_loss", "area_ha": 1.0} | kw
    return db.Event(status=status, status_utc=at, **fields)


def sent(kind: str, status: str, state: str = "sent") -> db.Notification:
    return db.Notification(kind=kind, event_status=status, state=state)


@pytest.mark.parametrize(
    ("e", "already", "expected"),
    [
        (event("pending"), {}, "alert"),
        (event("confirmed"), {}, "alert"),  # confirmed straight away
        (event("pending", datetime(2021, 4, 20, tzinfo=UTC)), {}, None),  # before watching
        (event("pending", SINCE), {}, None),  # that pass was in the archive already
        (event("pending", category="water"), {}, None),  # the rule says no
        (event("dismissed"), {}, None),  # gone before anyone was told
        (event("pending"), {"alert": sent("alert", "pending", "claimed")}, None),  # in flight
        (event("pending"), {"alert": sent("alert", "pending", "failed")}, "alert"),  # retry
        (event("pending"), {"alert": sent("alert", "pending")}, None),
        (event("confirmed"), {"alert": sent("alert", "pending")}, "update"),
        (event("dismissed"), {"alert": sent("alert", "pending")}, "update"),
        (event("seasonal"), {"alert": sent("alert", "confirmed")}, "update"),
        (event("confirmed"), {"alert": sent("alert", "confirmed")}, None),
        (event("pending"), {"alert": sent("alert", "confirmed")}, None),  # not a follow-up
        (
            event("seasonal"),
            {"alert": sent("alert", "pending"), "update": sent("update", "confirmed")},
            None,  # one follow-up only
        ),
    ],
)
def test_due_sends_one_alert_and_at_most_one_follow_up(e, already, expected):
    assert notify.due(e, RULE, SINCE, already) == expected


def test_nothing_is_due_with_alerts_off():
    assert notify.due(event("pending"), AlertRule(0.5, enabled=False), SINCE, {}) is None
