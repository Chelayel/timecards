from datetime import date

import pytest

from timecards import api, config, overrides
from conftest import TASK, WEEKDAYS, week_hours

WEEK = date(2026, 9, 28)
PREV = "2026-09-21"


def fill(sn, **kw):
    cards, log = sn.plan_week(WEEK, **kw)
    sn.save_cards(WEEK, cards)
    return log


def test_copies_previous_week_including_extra_hours(sn):
    sn.add(PREV, "task_work", {d: 7 for d in WEEKDAYS}, task=TASK)
    sn.add(PREV, "admin", {"monday": 1, "tuesday": 2, "wednesday": 1, "thursday": 1, "friday": 1},
           u_subcategory="general")
    log = fill(sn)
    assert f"source   copied from week of {PREV}" in log
    h = week_hours(sn, WEEK)
    assert h["general"]["tuesday"] == 2 and h["task_work"]["friday"] == 7
    assert fill(sn) and len(sn.week_cards(WEEK)) == 2  # idempotent


def test_skips_weeks_with_pto(sn):
    sn.add(PREV, "task_work", {"monday": 7}, task=TASK)
    sn.add(PREV, "admin", {"tuesday": 8}, u_subcategory="pto")
    sn.add("2026-09-14", "admin", {d: 1 for d in WEEKDAYS}, u_subcategory="general")
    log = fill(sn)
    assert "source   copied from week of 2026-09-14" in log
    assert set(week_hours(sn, WEEK)) == {"task_work", "general"}


def test_falls_back_to_defaults(sn):
    sn.db.clear()
    sn.add("2026-07-06", "admin", {"monday": 3}, u_subcategory="general")  # older than 8 weeks
    sn.add("2026-07-06", "task_work", {"monday": 1}, task=TASK)
    assert "source   from config defaults" in fill(sn)
    assert week_hours(sn, WEEK)["general"]["monday"] == 1


def test_defaults_flag(sn):
    sn.add(PREV, "admin", {"monday": 5}, u_subcategory="general")
    assert "source   from config defaults" in fill(sn, use_defaults=True)


def test_pto_and_changes_on_copied_week(sn):
    sn.add(PREV, "task_work", {d: 7 for d in WEEKDAYS}, task=TASK)
    sn.add(PREV, "admin", {d: 1 for d in WEEKDAYS}, u_subcategory="general")
    ch = overrides.parse(["mon pto 4, tue general +1, wed pto, fri build 6"])
    for _ in range(2):  # rerun must not add +1 twice
        fill(sn, changes=ch)
    h = week_hours(sn, WEEK)
    day = lambda d: {k: v[d] for k, v in h.items()}
    assert day("monday") == {"task_work": 3, "general": 1, "pto": 4}
    assert day("tuesday") == {"task_work": 7, "general": 2, "pto": 0}
    assert day("wednesday") == {"task_work": 0, "general": 0, "pto": 8}
    assert day("friday")["task_work"] == 6


def test_saved_partial_pto(sn):
    sn.add(PREV, "task_work", {d: 7 for d in WEEKDAYS}, task=TASK)
    sn.add(PREV, "admin", {d: 1 for d in WEEKDAYS}, u_subcategory="general")
    fill(sn, pto_dates={date(2026, 10, 1): 4.0})
    assert week_hours(sn, WEEK)["task_work"]["thursday"] == 3


def test_ambiguous_and_unknown_cards(sn):
    for bad in ["mon pto 4, tue admin +1", "tue nothing 3"]:
        with pytest.raises(api.ApiError):
            sn.plan_week(WEEK, changes=overrides.parse([bad]), use_defaults=True)


def test_week_snap_guard(sn):
    real = sn.request

    def snapping(method, path, params=None, json=None):
        if method == "POST":
            json = {**json, "week_starts_on": "2026-09-21"}
        return real(method, path, params, json)

    sn.request = snapping
    n = len(sn.db)
    with pytest.raises(api.ApiError, match="week_starts_on"):
        fill(sn, use_defaults=True)
    assert len(sn.db) == n


def test_parse_overrides_and_dates():
    assert [str(o) for o in overrides.parse(["wed pto, thu general -1, friday task_work 6"])] == \
        ["wed pto full day", "thu general -1", "fri task_work 6"]
    for bad in (["mo", "pto"], ["tue", "general"], ["tue"]):
        with pytest.raises(overrides.OverrideError):
            overrides.parse(bad)
    assert list(config.parse_dates(["2026-09-30..2026-10-04"], set(WEEKDAYS))) == \
        [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)]
    assert config.parse_dates(["2026-10-05:4"], set()) == {date(2026, 10, 5): 4.0}
    assert config.normalize_instance("acme") == "https://acme.service-now.com"
