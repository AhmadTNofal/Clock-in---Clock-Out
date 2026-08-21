"""Turning the raw event log into shifts, totals and a payroll CSV.

Timestamps are stored in UTC and converted to local time only for display and
reporting, so the twice-yearly BST/GMT change cannot corrupt stored data. A
shift is credited to the local date it *started*, which keeps a night shift
crossing midnight on one line instead of split across two days.

Unpaired events are always flagged. If somebody forgot to clock out and they are
on a shift whose end has already passed, they are assumed to have left at the
shift end - the paid figure uses that and the row says so, so payroll is not
held up by one forgotten scan. Without a shift (or while the shift is still
running) nothing is invented and the hours are left blank for a human to settle.

Paid hours are derived from actual hours by three rules, applied in this order:
the worked period is clipped to the employee's shift band (clock in early, paid
from the shift start; clock out late, paid to the shift end), the clipped times
are snapped to the 15-minute pay grid (in rounds forward, out rounds back, so
07:34 is paid from 07:45), and the shift's unpaid break is deducted - but only
when the paid time is long enough to have contained the break. Actual
times are always shown alongside so nothing is hidden from whoever runs payroll.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..extensions import db
from ..models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    AttendanceEvent,
    Employee,
    ShiftPattern,
    visible_employee_clause,
)

PAY_INTERVAL = dt.timedelta(minutes=15)


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


def round_forward(moment: dt.datetime) -> dt.datetime:
    """Snap forward to the next pay-grid boundary (07:34 -> 07:45)."""
    anchor = moment.replace(minute=0, second=0, microsecond=0)
    intervals = -((anchor - moment) // PAY_INTERVAL)  # ceiling division
    return anchor + intervals * PAY_INTERVAL


def round_back(moment: dt.datetime) -> dt.datetime:
    """Snap back to the previous pay-grid boundary (16:07 -> 16:00)."""
    anchor = moment.replace(minute=0, second=0, microsecond=0)
    return anchor + ((moment - anchor) // PAY_INTERVAL) * PAY_INTERVAL


def get_default_pattern() -> ShiftPattern | None:
    return db.session.scalars(
        select(ShiftPattern).where(ShiftPattern.is_default.is_(True))
    ).first()


@dataclass
class Shift:
    """One clock-in paired with its clock-out, if there is one."""

    employee: Employee
    clock_in: AttendanceEvent | None
    clock_out: AttendanceEvent | None
    tz: ZoneInfo
    issue: str | None = None
    pattern: ShiftPattern | None = None

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

    # --- paid time ----------------------------------------------------------
    def _band(self) -> tuple[dt.datetime, dt.datetime] | None:
        """The paid time band for this shift's local day, or None if no pattern."""
        anchor = self.start_local or self.end_local
        if self.pattern is None or anchor is None:
            return None
        day = anchor.date()
        band_start = dt.datetime.combine(day, self.pattern.start_time, tzinfo=self.tz)
        end_day = day + dt.timedelta(days=1) if self.pattern.crosses_midnight else day
        band_end = dt.datetime.combine(end_day, self.pattern.end_time, tzinfo=self.tz)
        return band_start, band_end

    @property
    def end_is_assumed(self) -> bool:
        """True when a missing clock-out is stood in for by the shift end.

        Only once the shift end has actually passed: somebody still on site
        mid-shift genuinely has no leaving time yet, assumed or otherwise.
        """
        if self.clock_out is not None or self.clock_in is None:
            return False
        band = self._band()
        return band is not None and band[1] <= dt.datetime.now(self.tz)

    @property
    def effective_end_local(self) -> dt.datetime | None:
        """The recorded clock-out, or the shift end when one can be assumed."""
        if self.end_local is not None:
            return self.end_local
        if self.end_is_assumed:
            return self._band()[1]  # type: ignore[index]
        return None

    @property
    def paid_start_local(self) -> dt.datetime | None:
        if self.start_local is None:
            return None
        band = self._band()
        start = max(self.start_local, band[0]) if band else self.start_local
        return round_forward(start)

    @property
    def paid_end_local(self) -> dt.datetime | None:
        end = self.effective_end_local
        if end is None:
            return None
        band = self._band()
        if band:
            end = min(end, band[1])
        return round_back(end)

    @property
    def paid_hours(self) -> float | None:
        """Hours to pay: band-clipped, grid-snapped, unpaid break deducted."""
        if self.is_complete and self.duration is None:
            return None  # clock-out precedes clock-in - a correction is needed
        start, end = self.paid_start_local, self.paid_end_local
        if start is None or end is None:
            return None
        seconds = (end - start).total_seconds()
        # The break comes off only when the paid time is long enough to have
        # contained it - a 2.5-hour afternoon stint has no lunch to deduct.
        if (
            self.pattern is not None
            and seconds > self.pattern.break_applies_after_minutes * 60
        ):
            seconds -= self.pattern.unpaid_break_minutes * 60
        return round(max(0.0, seconds) / 3600.0, 2)

    @property
    def display_issue(self) -> str | None:
        """The issue text, noting when the paid figure assumes the shift end."""
        if self.end_is_assumed:
            end = self._band()[1]  # type: ignore[index]
            return (
                "Did not clock out - assumed finished at their normal time "
                f"({end.strftime('%H:%M')})"
            )
        return self.issue


def pair_events(
    employee: Employee,
    events: list[AttendanceEvent],
    tz: ZoneInfo,
    pattern: ShiftPattern | None = None,
) -> list[Shift]:
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
                    Shift(
                        employee,
                        open_in,
                        None,
                        tz,
                        issue="No clock-out recorded",
                        pattern=pattern,
                    )
                )
            open_in = event
        elif event.direction == DIRECTION_OUT:
            if open_in is None:
                shifts.append(
                    Shift(
                        employee,
                        None,
                        event,
                        tz,
                        issue="No clock-in recorded",
                        pattern=pattern,
                    )
                )
            else:
                shift = Shift(employee, open_in, event, tz, pattern=pattern)
                if shift.duration is None:
                    shift.issue = "Clock-out precedes clock-in"
                shifts.append(shift)
                open_in = None

    if open_in is not None:
        shifts.append(
            Shift(employee, open_in, None, tz, issue="Still clocked in", pattern=pattern)
        )

    return shifts


def build_timesheet(
    start: dt.date,
    end: dt.date,
    tz: ZoneInfo,
    *,
    employee_id: int | None = None,
    department: str | None = None,
    include_inactive: bool = False,
) -> list[Shift]:
    """Build shifts for a local date range, ordered by employee then time."""
    first, last = local_range_bounds(start, end, tz)

    employee_stmt = (
        select(Employee)
        .where(visible_employee_clause())
        .order_by(Employee.last_name, Employee.first_name)
    )
    if employee_id is not None:
        employee_stmt = employee_stmt.where(Employee.id == employee_id)
    else:
        if not include_inactive:
            employee_stmt = employee_stmt.where(Employee.is_active.is_(True))
        if department:
            employee_stmt = employee_stmt.where(Employee.department == department)
    employees = db.session.scalars(employee_stmt).all()

    default_pattern = get_default_pattern()
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
        pattern = employee.shift_pattern or default_pattern
        shifts.extend(pair_events(employee, list(events), tz, pattern=pattern))

    return shifts


def list_departments() -> list[str]:
    """Distinct non-empty department names, for the timesheet filter."""
    rows = db.session.scalars(
        select(Employee.department)
        .where(Employee.department.is_not(None), visible_employee_clause())
        .distinct()
        .order_by(Employee.department)
    ).all()
    return [row for row in rows if row]


@dataclass
class EmployeeTotal:
    employee: Employee
    hours: float
    paid_hours: float
    shifts: int
    issues: int
    issue_details: list[str] = field(default_factory=list)


def summarise(shifts: list[Shift]) -> list[EmployeeTotal]:
    """Total hours per employee, with the exact rows needing attention.

    Each detail names the day and what is wrong, so management can spot a
    discrepancy on the master sheet without opening every timesheet.
    """
    buckets: dict[int, EmployeeTotal] = {}
    for shift in shifts:
        total = buckets.get(shift.employee.id)
        if total is None:
            total = EmployeeTotal(shift.employee, 0.0, 0.0, 0, 0)
            buckets[shift.employee.id] = total
        if shift.hours is not None:
            total.hours = round(total.hours + shift.hours, 2)
            total.shifts += 1
        if shift.paid_hours is not None:
            total.paid_hours = round(total.paid_hours + shift.paid_hours, 2)
        if shift.issue:
            total.issues += 1
            day = shift.date.strftime("%a %d/%m") if shift.date else "unknown day"
            total.issue_details.append(f"{day}: {shift.display_issue}")
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
            "Shift",
            "Paid from",
            "Paid to",
            "Paid hours",
            "Notes",
        ]
    )
    for shift in shifts:
        start = shift.start_local
        end = shift.end_local
        paid_start = shift.paid_start_local if shift.paid_hours is not None else None
        paid_end = shift.paid_end_local if shift.paid_hours is not None else None
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
                shift.pattern.name if shift.pattern else "",
                paid_start.strftime("%H:%M") if paid_start else "",
                paid_end.strftime("%H:%M") if paid_end else "",
                f"{shift.paid_hours:.2f}" if shift.paid_hours is not None else "",
                shift.display_issue or "",
            ]
        )
    return buffer.getvalue()


def to_master_csv(totals: list[EmployeeTotal]) -> str:
    """One line per person with their total paid hours - the payroll master sheet.

    Deliberately terse: management scan this for anything that looks wrong, then
    open that person's individual timesheet for the day-by-day detail.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Payroll ref",
            "Surname",
            "First name",
            "Department",
            "Shift",
            "Clocked hours",
            "Paid hours",
            "Rows needing attention",
        ]
    )
    for total in totals:
        writer.writerow(
            [
                total.employee.payroll_ref,
                total.employee.last_name,
                total.employee.first_name,
                total.employee.department or "",
                total.employee.shift_pattern.name if total.employee.shift_pattern else "",
                f"{total.hours:.2f}",
                f"{total.paid_hours:.2f}",
                "; ".join(total.issue_details),
            ]
        )
    return buffer.getvalue()
