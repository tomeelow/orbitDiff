"""Alert rules: whether a change event should reach a user.

`should_alert` is the one evaluator. Sending, the web app's "N of M past changes would have
alerted" preview and the evaluation set's production score all go through it; nothing
re-implements the rule.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from orbitdiff import db
from orbitdiff.config import AlertsConfig

# What users hear about by default, as in the mockup. Water is opt-in (floods and reservoir
# levels swing), and so is "other": regrowth, a dry year after a flooded one, changes no
# heuristic recognises.
ALERT_CATEGORIES = frozenset({"buildings_roads", "vegetation_loss", "solar_industry"})


@dataclass(frozen=True)
class AlertRule:
    min_ha: float  # never below the minimum mapping unit
    enabled: bool = True
    categories: frozenset[str] = ALERT_CATEGORIES
    skip_seasonal: bool = True


def default_rule(cfg: AlertsConfig) -> AlertRule:
    return AlertRule(min_ha=cfg.default_min_ha)


def rule_of(row: db.AlertRule | None, cfg: AlertsConfig) -> AlertRule:
    """An AOI's stored rule; the default settings, switched off, when it has none."""
    if row is None:
        return AlertRule(min_ha=cfg.default_min_ha, enabled=False)
    return AlertRule(
        min_ha=row.min_ha,
        enabled=row.enabled,
        categories=frozenset(row.categories),
        skip_seasonal=row.skip_seasonal,
    )


def should_alert(status: str, category: str, area_ha: float, rule: AlertRule) -> tuple[bool, str]:
    """Whether an event in this state alerts under `rule`, and why not when it doesn't."""
    if not rule.enabled:
        return False, "alerts off"
    if status == "dismissed":
        return False, "dismissed"
    if status == "seasonal" and rule.skip_seasonal:
        return False, "seasonal"
    if category not in rule.categories:
        return False, "category off"
    if area_ha < rule.min_ha:
        return False, f"under {rule.min_ha:g} ha"
    return True, "alert"


def would_alert(
    statuses: Sequence[str], category: str, area_ha: float, rule: AlertRule
) -> tuple[bool, str]:
    """Whether an event alerted at any point of its history (oldest status first): the
    alert on first sight goes out before a later pass can dismiss it, so a false alarm
    still counts. Why not, from its latest status, when it never would have."""
    verdicts = [should_alert(status, category, area_ha, rule) for status in statuses]
    return next((v for v in verdicts if v[0]), verdicts[-1])
