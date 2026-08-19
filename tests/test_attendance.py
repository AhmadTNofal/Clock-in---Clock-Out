"""Clock-in / clock-out rules: alternation, cooldown and voiding."""

from __future__ import annotations

import datetime as dt

from app.models import DIRECTION_IN, DIRECTION_OUT, AttendanceEvent, utcnow
from app.services import attendance

from .conftest import make_employee


def test_first_scan_clocks_in(db):
    employee = make_employee(db)
    assert attendance.next_direction(employee.id) == DIRECTION_IN

    result = attendance.record_clock(employee, cooldown_seconds=0)
    assert result.recorded
    assert result.direction == DIRECTION_IN
    assert "Clocked in" in result.message


def test_directions_alternate(db):
    employee = make_employee(db)
    attendance.record_clock(employee, cooldown_seconds=0)
    assert attendance.next_direction(employee.id) == DIRECTION_OUT

    second = attendance.record_clock(employee, cooldown_seconds=0)
    assert second.direction == DIRECTION_OUT
    assert attendance.next_direction(employee.id) == DIRECTION_IN


def test_cooldown_suppresses_a_repeat_scan(db):
    employee = make_employee(db)
    first = attendance.record_clock(employee, cooldown_seconds=90)
    assert first.recorded

    # Same direction moments later: reported back, not written again.
    repeat = attendance.record_clock(
        employee, direction=DIRECTION_IN, cooldown_seconds=90
    )
    assert not repeat.recorded
    assert repeat.duplicate_of is not None
    assert repeat.duplicate_of.id == first.event.id
    assert "Already clocked in" in repeat.message
    assert db.session.query(AttendanceEvent).count() == 1


def test_cooldown_does_not_block_the_opposite_direction(db):
    """Somebody who arrives and immediately leaves must still be able to clock out."""
    employee = make_employee(db)
    attendance.record_clock(employee, cooldown_seconds=90)
    out = attendance.record_clock(employee, direction=DIRECTION_OUT, cooldown_seconds=90)
    assert out.recorded
    assert out.direction == DIRECTION_OUT


def test_cooldown_expires(db):
    employee = make_employee(db)
    old = utcnow() - dt.timedelta(seconds=200)
    attendance.record_clock(
        employee, direction=DIRECTION_IN, occurred_at=old, cooldown_seconds=90
    )
    again = attendance.record_clock(
        employee, direction=DIRECTION_IN, cooldown_seconds=90
    )
    assert again.recorded


def test_is_clocked_in_tracks_state(db):
    employee = make_employee(db)
    assert not attendance.is_clocked_in(employee.id)
    attendance.record_clock(employee, cooldown_seconds=0)
    assert attendance.is_clocked_in(employee.id)
    attendance.record_clock(employee, cooldown_seconds=0)
    assert not attendance.is_clocked_in(employee.id)


def test_voided_event_is_ignored_by_state(db, admin):
    employee = make_employee(db)
    result = attendance.record_clock(employee, cooldown_seconds=0)
    attendance.void_event(result.event, admin_id=admin.id, reason="Scanned by mistake")

    assert not attendance.is_clocked_in(employee.id)
    assert attendance.next_direction(employee.id) == DIRECTION_IN
    # The row itself survives for the audit trail.
    assert db.session.query(AttendanceEvent).count() == 1
    assert "Scanned by mistake" in result.event.note


def test_currently_on_site_lists_only_clocked_in_people(db):
    alice = make_employee(db, ref="E001", first="Alice")
    bob = make_employee(db, ref="E002", first="Bob")
    make_employee(db, ref="E003", first="Carol", is_active=False)

    attendance.record_clock(alice, cooldown_seconds=0)
    attendance.record_clock(bob, cooldown_seconds=0)
    attendance.record_clock(bob, cooldown_seconds=0)  # Bob clocks out again

    on_site = attendance.currently_on_site()
    assert [person.first_name for person in on_site] == ["Alice"]


def test_invalid_direction_is_rejected(db):
    employee = make_employee(db)
    try:
        attendance.record_clock(employee, direction="sideways")
    except ValueError as exc:
        assert "direction must be one of" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("An invalid direction should raise ValueError")


def test_lingering_at_the_kiosk_does_not_clock_you_back_out(db):
    """An automatic scan inside the cooldown must not alternate.

    Regression test: next_direction always returns the opposite of the last
    entry, so a cooldown that only compared directions never fired on the
    automatic path - two scans in a row clocked the person in and straight out.
    """
    employee = make_employee(db)
    first = attendance.record_clock(employee, cooldown_seconds=90)
    assert first.recorded and first.direction == DIRECTION_IN

    second = attendance.record_clock(employee, cooldown_seconds=90)
    assert not second.recorded
    assert second.direction == DIRECTION_IN  # reports the state they are in
    assert second.duplicate_of.id == first.event.id
    assert "Already clocked in" in second.message
    assert db.session.query(AttendanceEvent).count() == 1


def test_automatic_scan_alternates_once_the_cooldown_expires(db):
    employee = make_employee(db)
    attendance.record_clock(
        employee, occurred_at=utcnow() - dt.timedelta(seconds=200), cooldown_seconds=90
    )
    out = attendance.record_clock(employee, cooldown_seconds=90)
    assert out.recorded
    assert out.direction == DIRECTION_OUT


def test_explicit_clock_out_still_works_inside_the_cooldown(db):
    """Pressing Clock out states intent, so it is honoured immediately."""
    employee = make_employee(db)
    attendance.record_clock(employee, cooldown_seconds=90)
    out = attendance.record_clock(employee, direction=DIRECTION_OUT, cooldown_seconds=90)
    assert out.recorded
    assert out.direction == DIRECTION_OUT
    assert db.session.query(AttendanceEvent).count() == 2
