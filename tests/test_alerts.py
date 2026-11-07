"""The one alert-rule evaluator: every reason an event doesn't reach a user."""

import pytest

from orbitdiff.alerts import AlertRule, default_rule, should_alert
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
