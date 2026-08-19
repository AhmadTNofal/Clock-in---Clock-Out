"""Pairing events into shifts, totals, CSV, and the BST/GMT boundary."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.models import DIRECTION_IN, DIRECTION_OUT, AttendanceEvent
from app.services.timesheet import (
    build_timesheet,
    local_day_bounds,
    pair_events,
    summarise,
    to_csv,
    to_local,
)

from .conftest import make_employee

LONDON = ZoneInfo("Europe/London")


def _event(employee, direction, moment, voided=False):
    return AttendanceEvent(
        employee_id=employee.id,
        direction=direction,
        occurred_at=moment,
        is_voided=voided,
    )


def _utc(year, month, day, hour, minute=0):
    return dt.datetime(year, month, day, hour, minute)


# --- pairing ------------------------------------------------------------------
def test_complete_shift_gives_hours(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    shifts = pair_events(employee, events, LONDON)

    assert len(shifts) == 1
    assert shifts[0].is_complete
    assert shifts[0].hours == pytest.approx(8.5)
    assert shifts[0].issue is None


def test_missing_clock_out_is_flagged_not_guessed(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 13, 7, 30)),
    ]
    shifts = pair_events(employee, events, LONDON)

    assert len(shifts) == 2
    assert shifts[0].issue == "No clock-out recorded"
    assert shifts[0].hours is None  # never invented
    assert shifts[1].issue == "Still clocked in"


def test_orphan_clock_out_is_reported(db):
    employee = make_employee(db)
    shifts = pair_events(
        employee, [_event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0))], LONDON
    )
    assert shifts[0].issue == "No clock-in recorded"
    assert shifts[0].hours is None


def test_voided_events_are_skipped(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 0), voided=True),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    shifts = pair_events(employee, events, LONDON)
    assert len(shifts) == 1
    assert shifts[0].hours == pytest.approx(8.5)


def test_night_shift_credited_to_the_starting_day(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 22, 0)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 13, 6, 0)),
    ]
    shifts = pair_events(employee, events, LONDON)
    assert len(shifts) == 1
    assert shifts[0].date == dt.date(2026, 1, 12)
    assert shifts[0].hours == pytest.approx(8.0)


# --- timezone handling --------------------------------------------------------
def test_utc_is_converted_to_british_summer_time():
    """A 07:30 UTC arrival in July is 08:30 on the shop floor."""
    local = to_local(_utc(2026, 7, 1, 7, 30), LONDON)
    assert local.strftime("%H:%M") == "08:30"
    # In winter, UTC and local agree.
    assert to_local(_utc(2026, 1, 1, 7, 30), LONDON).strftime("%H:%M") == "07:30"


def test_local_day_bounds_follow_the_clock_change():
    """A summer local day starts at 23:00 UTC the previous evening."""
    start, end = local_day_bounds(dt.date(2026, 7, 1), LONDON)
    assert start == dt.datetime(2026, 6, 30, 23, 0)
    assert end == dt.datetime(2026, 7, 1, 23, 0)

    winter_start, winter_end = local_day_bounds(dt.date(2026, 1, 15), LONDON)
    assert winter_start == dt.datetime(2026, 1, 15, 0, 0)
    assert winter_end == dt.datetime(2026, 1, 16, 0, 0)


def test_timesheet_query_respects_local_day_boundaries(db):
    """An 08:00 BST clock-in must appear on that local day, not the day before."""
    employee = make_employee(db)
    db.session.add_all(
        [
            _event(employee, DIRECTION_IN, _utc(2026, 7, 1, 7, 0)),  # 08:00 local
            _event(employee, DIRECTION_OUT, _utc(2026, 7, 1, 15, 0)),  # 16:00 local
        ]
    )
    db.session.commit()

    shifts = build_timesheet(dt.date(2026, 7, 1), dt.date(2026, 7, 1), LONDON)
    assert len(shifts) == 1
    assert shifts[0].start_local.strftime("%H:%M") == "08:00"
    assert shifts[0].hours == pytest.approx(8.0)

    # The previous day must be empty.
    assert build_timesheet(dt.date(2026, 6, 30), dt.date(2026, 6, 30), LONDON) == []


def test_unknown_timezone_falls_back_to_utc():
    from app.services.timesheet import get_timezone

    assert str(get_timezone("Mars/Olympus_Mons")) == "UTC"


# --- summaries and export -----------------------------------------------------
def test_summarise_totals_hours_and_counts_issues(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 0)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 15, 0)),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 13, 7, 0)),  # never clocked out
    ]
    totals = summarise(pair_events(employee, events, LONDON))

    assert len(totals) == 1
    assert totals[0].hours == pytest.approx(8.0)
    assert totals[0].shifts == 1
    assert totals[0].issues == 1


def test_csv_has_a_header_and_one_row_per_shift(db):
    employee = make_employee(db, ref="E010", first="Nia", last="Owens")
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    csv_text = to_csv(pair_events(employee, events, LONDON))
    lines = csv_text.strip().split("\r\n")

    assert lines[0].startswith("Payroll ref,Surname,First name")
    assert len(lines) == 2
    assert "E010" in lines[1]
    assert "Owens" in lines[1]
    assert "07:30" in lines[1]
    assert "8.50" in lines[1]


def test_csv_leaves_hours_blank_for_an_unpaired_shift(db):
    employee = make_employee(db)
    events = [_event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30))]
    row = to_csv(pair_events(employee, events, LONDON)).strip().split("\r\n")[1]
    assert row.endswith("Still clocked in")
    assert ",," in row  # blank clock-out and blank hours
