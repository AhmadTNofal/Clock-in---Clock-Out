"""Turning the raw event log into shifts, totals and a payroll CSV.

Timestamps are stored in UTC and converted to local time only for display and
reporting, so the twice-yearly BST/GMT change cannot corrupt stored data. A
shift is credited to the local date it *started*, which keeps a night shift
crossing midnight on one line instead of split across two days.

Unpaired events are reported, never guessed at. If somebody forgot to clock out,
the row says so and the hours are left blank for a human to settle - inventing a
leaving time would put a wrong figure into someone's pay.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..extensions import db
from ..models import DIRECTION_IN, DIRECTION_OUT, AttendanceEvent, Employee


def get_timezone(name: str) -> ZoneInfo:
    """Load a timezone, falling back to UTC if the name is unknown."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - bad config should not take the app down
        return ZoneInfo("UTC")


def to_local(moment: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """Interpret a naive UTC timestamp and return it in *tz*."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(tz)


def local_day_bounds(day: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """Naive UTC start/end covering one local calendar day."""
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return (
        start_local.astimezone(dt.timezone.utc).replace(tzinfo=None),
        end_local.astimezone(dt.timezone.utc).replace(tzinfo=None),
    )


def local_range_bounds(
    start: dt.date, end: dt.date, tz: ZoneInfo
) -> tuple[dt.datetime, dt.datetime]:
    """Naive UTC bounds covering local days *start* to *end* inclusive."""
    first, _ = local_day_bounds(start, tz)
    _, last = local_day_bounds(end, tz)
    return first, last


@dataclass
class Shift:
    """One clock-in paired with its clock-out, if there is one."""

    employee: Employee
    clock_in: AttendanceEvent | None
    clock_out: AttendanceEvent | None
    tz: ZoneInfo
    issue: str | None = None

    @property
    def start_local(self) -> dt.datetime | None:
        return to_local(self.clock_in.occurred_at, self.tz) if self.clock_in else None

    @property
    def end_local(self) -> dt.datetime | None:
        return to_local(self.clock_out.occurred_at, self.tz) if self.clock_out else None

    @property
    def date(self) -> dt.date | None:
        anchor = self.start_local or self.end_local
        return anchor.date() if anchor else None

    @property
    def is_complete(self) -> bool:
        return self.clock_in is not None and self.clock_out is not None

    @property
    def duration(self) -> dt.timedelta | None:
        if not self.is_complete:
            return None
        delta = self.clock_out.occurred_at - self.clock_in.occurred_at  # type: ignore[union-attr]
        return delta if delta.total_seconds() >= 0 else None

    @property
    def hours(self) -> float | None:
        duration = self.duration
        return round(duration.total_seconds() / 3600.0, 2) if duration else None


def pair_events(employee: Employee, events: list[AttendanceEvent], tz: ZoneInfo) -> list[Shift]:
    """Pair a chronological event list into shifts.

    The log is a plain alternating sequence in the normal case. The two ways it
    breaks are handled explicitly: an IN followed by another IN (forgot to clock
    out) and an OUT with no matching IN (forgot to clock in, or the shift started
    before the reporting window).
    """
    shifts: list[Shift] = []
    open_in: AttendanceEvent | None = None

    for event in events:
        if event.is_voided:
            continue
        if event.direction == DIRECTION_IN:
            if open_in is not None:
                shifts.append(
                    Shift(employee, open_in, None, tz, issue="No clock-out recorded")
                )
            open_in = event
        elif event.direction == DIRECTION_OUT:
            if open_in is None:
                shifts.append(
                    Shift(employee, None, event, tz, issue="No clock-in recorded")
                )
            else:
                shift = Shift(employee, open_in, event, tz)
                if shift.duration is None:
                    shift.issue = "Clock-out precedes clock-in"
                shifts.append(shift)
                open_in = None

    if open_in is not None:
        shifts.append(Shift(employee, open_in, None, tz, issue="Still clocked in"))

    return shifts


def build_timesheet(
    start: dt.date,
    end: dt.date,
    tz: ZoneInfo,
    *,
    employee_id: int | None = None,
    include_inactive: bool = False,
) -> list[Shift]:
    """Build shifts for a local date range, ordered by employee then time."""
    first, last = local_range_bounds(start, end, tz)

    employee_stmt = select(Employee).order_by(Employee.last_name, Employee.first_name)
    if employee_id is not None:
        employee_stmt = employee_stmt.where(Employee.id == employee_id)
    elif not include_inactive:
        employee_stmt = employee_stmt.where(Employee.is_active.is_(True))
    employees = db.session.scalars(employee_stmt).all()

    shifts: list[Shift] = []
    for employee in employees:
        events = db.session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.employee_id == employee.id,
                AttendanceEvent.is_voided.is_(False),
                AttendanceEvent.occurred_at >= first,
                AttendanceEvent.occurred_at < last,
            )
            .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
        ).all()
        shifts.extend(pair_events(employee, list(events), tz))

    return shifts


@dataclass
class EmployeeTotal:
    employee: Employee
    hours: float
    shifts: int
    issues: int


def summarise(shifts: list[Shift]) -> list[EmployeeTotal]:
    """Total hours per employee, with a count of rows needing attention."""
    buckets: dict[int, EmployeeTotal] = {}
    for shift in shifts:
        total = buckets.get(shift.employee.id)
        if total is None:
            total = EmployeeTotal(shift.employee, 0.0, 0, 0)
            buckets[shift.employee.id] = total
        if shift.hours is not None:
            total.hours = round(total.hours + shift.hours, 2)
            total.shifts += 1
        if shift.issue:
            total.issues += 1
    return sorted(
        buckets.values(), key=lambda t: (t.employee.last_name, t.employee.first_name)
    )


def to_csv(shifts: list[Shift]) -> str:
    """Render shifts as CSV for payroll. Excel-friendly (CRLF, ISO dates)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Payroll ref",
            "Surname",
            "First name",
            "Department",
            "Date",
            "Clock in",
            "Clock out",
            "Hours",
            "Notes",
        ]
    )
    for shift in shifts:
        start = shift.start_local
        end = shift.end_local
        writer.writerow(
            [
                shift.employee.payroll_ref,
                shift.employee.last_name,
                shift.employee.first_name,
                shift.employee.department or "",
                shift.date.isoformat() if shift.date else "",
                start.strftime("%H:%M") if start else "",
                end.strftime("%H:%M") if end else "",
                f"{shift.hours:.2f}" if shift.hours is not None else "",
                shift.issue or "",
            ]
        )
    return buffer.getvalue()
