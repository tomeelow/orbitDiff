"""Evaluation: label files, matching detections to events, and the metrics users feel."""

import json

import numpy as np
import pytest
from shapely.geometry import box, mapping

from orbitdiff.alerts import AlertRule
from orbitdiff.config import pipeline_config
from orbitdiff.detection import Event
from orbitdiff.evaluation import (
    Alert,
    AoiRun,
    EvalAoi,
    Label,
    Scores,
    alerts_from,
    dump_aoi,
    f1,
    gate,
    load_eval_set,
    metrics,
    parse_aoi,
    score,
    table,
)

CFG = pipeline_config().evaluation
SITE = box(0, 0, 200, 200)  # a labelled event's footprint, in UTM metres
ELSEWHERE = box(1000, 1000, 1200, 1200)


def aoi(labels=(), hard_cases=(), first="2020-01", last="2022-12") -> EvalAoi:
    # 36 months of imagery, 24 of them with an anniversary: two AOI-years.
    return EvalAoi("x", box(0, 0, 1, 1), first, last, tuple(hard_cases), True, tuple(labels))


def alert(footprint, month: str, day: str, category="vegetation_loss") -> Alert:
    return Alert(footprint, category, month, np.datetime64(day))


def test_scores_count_events_false_alerts_and_delay():
    label = Label("vegetation_loss", "2021-05", SITE)
    run = AoiRun(
        aoi([label], hard_cases=["cropland"]),
        (SITE,),
        (np.datetime64("2021-05-03"),),
        (
            alert(box(50, 50, 250, 250), "2021-05", "2021-05-13"),  # the clearing, 10 days on
            alert(ELSEWHERE, "2021-07", "2021-07-02"),  # nothing there: a false alert
            alert(box(0, 0, 100, 100), "2021-06", "2021-06-01"),  # a fragment of the same one
        ),
    )
    quiet = AoiRun(aoi(hard_cases=["cropland"]), (), (), (alert(SITE, "2021-08", "2021-08-01"),))

    report = score([run, quiet], CFG)

    s = report.overall
    assert (s.alerts, s.true_alerts, s.labels, s.found) == (4, 2, 1, 1)
    assert s.precision == 0.5 and s.recall == 1.0
    assert s.false_alerts_per_aoi_year == pytest.approx(2 / 4)  # 2 false over 2 AOIs x 2 years
    assert s.median_delay_days == 10  # from the first clear pass to the earliest matching alert
    assert report.by_hard_case["cropland"].false_alerts_per_aoi_year == pytest.approx(0.5)
    assert report.by_category["vegetation_loss"].recall == 1.0
    assert report.by_category["water"].recall is None
    assert metrics(report)["recall"] == 1.0
    assert "| naive | 0.50 | 1.00 | 0.50 | 10 |" in table({"naive": report})


@pytest.mark.parametrize(
    ("month", "footprint", "found"),
    [
        ("2021-04", SITE, True),  # a month early: labels are monthly guesses
        ("2021-03", SITE, False),  # too early to be this event
        ("2022-05", SITE, True),  # late, but the same event; the delay says how late
        ("2022-06", SITE, False),
        ("2021-05", box(150, 150, 400, 400), False),  # touches it, overlap under a quarter
    ],
)
def test_matching_needs_overlap_and_a_month_within_tolerance(month, footprint, found):
    label = Label("vegetation_loss", "2021-05", SITE)
    run = AoiRun(aoi([label]), (SITE,), (None,), (alert(footprint, month, f"{month}-15"),))
    assert score([run], CFG).overall.found == found


def test_eval_files_load_and_any_edit_changes_the_version(tmp_path):
    def write(month: str) -> None:
        features = [
            {
                "type": "Feature",
                "geometry": mapping(box(13.77, 52.38, 13.82, 52.40)),
                "properties": {
                    "role": "aoi",
                    "first_month": "2017-01",
                    "last_month": "2024-12",
                    "hard_cases": ["forest"],
                },
            },
            {
                "type": "Feature",
                "geometry": mapping(box(13.79, 52.39, 13.80, 52.395)),
                "properties": {"role": "event", "category": "vegetation_loss", "month": month},
            },
        ]
        (tmp_path / "site.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": features})
        )

    write("2020-02")
    (site,), version = load_eval_set(tmp_path)
    assert site.labels[0].month == "2020-02" and site.hard_cases == ("forest",)
    assert not site.verified  # no verified_by: never reported as a verified result
    assert site.evaluated_months == 96 - 12
    write("2020-03")
    assert load_eval_set(tmp_path)[1] != version


def site(*events: tuple[str, str]) -> dict:
    """An evaluation file's contents: 2017-01 to 2024-12, with (category, month) changes."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(box(13.77, 52.38, 13.82, 52.40)),
                "properties": {"role": "aoi", "first_month": "2017-01", "last_month": "2024-12"},
            },
            *(
                {
                    "type": "Feature",
                    "geometry": mapping(box(13.79, 52.39, 13.80, 52.395)),
                    "properties": {"role": "event", "category": category, "month": month},
                }
                for category, month in events
            ),
        ],
    }


@pytest.mark.parametrize(
    ("category", "month", "problem"),
    [
        ("vegetation_loss", "2017-06", "first year"),  # no anniversary: no method can find it
        ("vegetation_loss", "2025-01", "not in 2017-01 to 2024-12"),
        ("vegetation_loss", "2020-13", "not in"),
        ("other", "2020-02", "unknown category"),  # a label says what changed
    ],
)
def test_eval_files_refuse_labels_that_cant_be_scored(category, month, problem):
    with pytest.raises(ValueError, match=problem):
        parse_aoi("site", site(("buildings_roads", "2018-01"), (category, month)))


def test_an_eval_file_is_written_one_feature_per_line_and_reads_back():
    text = dump_aoi(site(("buildings_roads", "2018-01"), ("water", "2024-12")))
    assert len(text.splitlines()) == 2 + 3  # header, the area, two changes, footer
    aoi = parse_aoi("site", json.loads(text))
    assert [(label.category, label.month) for label in aoi.labels] == [
        ("buildings_roads", "2018-01"),
        ("water", "2024-12"),
    ]


def event(category="vegetation_loss", area_px=100, history=()) -> Event:
    pixels = np.zeros((20, 20), bool)
    pixels.flat[:area_px] = True
    e = Event(
        "production",
        "2021-05",
        "2020-05",
        pixels,
        SITE,
        category,
        0.8,
        0.2,
        np.datetime64("2021-05-05"),
    )
    for status, at in history:
        e.set(status, "pass", np.datetime64(at))
    return e


def test_production_alerts_follow_the_rule_in_the_status_they_had_then():
    rule = AlertRule(min_ha=0.5)
    confirmed = event(history=[("pending", "2021-05-05"), ("confirmed", "2021-05-20")])
    dismissed = event(history=[("pending", "2021-05-05"), ("dismissed", "2021-05-20")])
    seasonal = event(category="water", history=[("seasonal", "2021-05-05")])
    small = event(area_px=40, history=[("pending", "2021-05-05")])  # 0.4 ha
    events = [confirmed, dismissed, seasonal, small]

    when_pending = alerts_from(events, rule)
    when_confirmed = alerts_from(events, rule, confirmed_only=True)

    # Alerting on first sight tells users about the dismissed one too: a false alert.
    assert [a.alerted for a in when_pending] == [np.datetime64("2021-05-05")] * 2
    assert [a.alerted for a in when_confirmed] == [np.datetime64("2021-05-20")]
    assert len(alerts_from(events)) == 4  # a bare detector: everything, when detected


def scores(alerts: int, true_alerts: int, labels: int, found: int, aoi_years: float = 10):
    return Scores(alerts, true_alerts, labels, found, aoi_years)


CHAMPION = scores(alerts=20, true_alerts=10, labels=20, found=10)  # 1.0 false alerts / AOI-yr
BASELINE = scores(alerts=10, true_alerts=4, labels=20, found=4)  # F1 0.27


def test_a_candidate_that_finds_more_without_more_false_alerts_is_promoted():
    candidate = scores(alerts=22, true_alerts=12, labels=20, found=12)
    assert gate(candidate, CHAMPION, BASELINE, recall_tolerance=0.02) == []


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (scores(25, 14, 20, 14), "false alerts per AOI-year rise from 1.00 to 1.10"),
        (scores(15, 9, 20, 9), "event recall falls from 0.50 to 0.45"),
        (scores(4, 1, 20, 1), "doesn't beat the naive baseline"),
    ],
)
def test_the_gate_refuses_a_candidate_with_the_reason(candidate, reason):
    reasons = gate(candidate, CHAMPION, BASELINE, recall_tolerance=0.02)
    assert any(reason in r for r in reasons), reasons


def test_the_gate_allows_a_recall_loss_within_the_tolerance():
    champion = scores(alerts=20, true_alerts=10, labels=100, found=50)
    candidate = scores(alerts=19, true_alerts=10, labels=100, found=49)  # recall 0.50 -> 0.49
    assert gate(candidate, champion, BASELINE, recall_tolerance=0.02) == []


def test_the_gate_refuses_what_the_evaluation_set_cant_score():
    empty = Scores()  # no labelled changes, no AOI-years
    assert gate(empty, empty, empty, 0.02) == [
        "the evaluation set can't score it: it needs AOI-years and labelled changes"
    ]


def test_f1_is_zero_with_labels_and_no_alerts_and_unknown_without_labels():
    assert f1(scores(0, 0, 10, 0)) == 0.0
    assert f1(Scores()) is None
    assert f1(scores(10, 5, 10, 5)) == pytest.approx(0.5)
