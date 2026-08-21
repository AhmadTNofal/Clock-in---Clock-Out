"""Create (or remove) obviously-fake data for trying out timesheets and shifts.

    python scripts/seed_demo_data.py            # add the demo employees + clockings
    python scripts/seed_demo_data.py --remove   # delete them again

Every demo employee has a payroll reference starting with DEMO, so they are easy
to spot and easy to delete (removing the employee cascades to their events).
Real records are never touched. Safe to re-run: it refuses to add a second copy.

The generated month of clockings deliberately includes the awkward cases the
timesheet has to handle: early arrivals, late finishes, a forgotten clock-out,
odd minutes that exercise the 15-minute pay grid, and one department on an
early shift.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import current_app  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    DIRECTION_IN,
    DIRECTION_OUT,
    METHOD_MANUAL,
    AttendanceEvent,
    Employee,
    ShiftPattern,
)
from app.services.timesheet import get_timezone  # noqa: E402

DEMO_NOTE = "Demo data (scripts/seed_demo_data.py)"
DEMO_SHIFT_NAME = "Earlies (demo)"

DEMO_EMPLOYEES = [
    # (payroll_ref, first, last, department, on_early_shift)
    ("DEMO01", "Alice", "Turner", "Assembly", False),
    ("DEMO02", "Bob", "Ward", "Assembly", False),
    ("DEMO03", "Carys", "Evans", "Assembly", True),
    ("DEMO04", "Dev", "Patel", "Joinery", False),
    ("DEMO05", "Erin", "Hughes", "Joinery", False),
    ("DEMO06", "Frank", "Osei", "Joinery", True),
    ("DEMO07", "Grace", "Lam", "Office", False),
    ("DEMO08", "Harry", "Booth", "Office", False),
]

WEEKS_OF_HISTORY = 5  # a little more than the default four-week window


def local_to_utc(moment: dt.datetime, tz) -> dt.datetime:
    return moment.replace(tzinfo=tz).astimezone(dt.timezone.utc).replace(tzinfo=None)


def add_demo_data() -> None:
    existing = db.session.scalars(
        select(Employee).where(Employee.payroll_ref.like("DEMO%"))
    ).first()
    if existing is not None:
        print("Demo employees already present - nothing added. Use --remove first.")
        return

    tz = get_timezone(current_app.config["TIMEZONE"])

    early_shift = db.session.scalars(
        select(ShiftPattern).where(ShiftPattern.name == DEMO_SHIFT_NAME)
    ).first()
    if early_shift is None:
        early_shift = ShiftPattern(
            name=DEMO_SHIFT_NAME,
            start_time=dt.time(6, 0),
            end_time=dt.time(14, 0),
            unpaid_break_minutes=30,
        )
        db.session.add(early_shift)
        db.session.flush()

    employees: list[tuple[Employee, bool]] = []
    for ref, first, last, department, on_earlies in DEMO_EMPLOYEES:
        employee = Employee(
            payroll_ref=ref,
            first_name=first,
            last_name=last,
            department=department,
            shift_pattern_id=early_shift.id if on_earlies else None,
        )
        db.session.add(employee)
        employees.append((employee, on_earlies))
    db.session.flush()

    rng = random.Random(42)  # seeded so re-seeding gives the same month again
    today = dt.datetime.now(tz).date()
    start_day = today - dt.timedelta(weeks=WEEKS_OF_HISTORY)
    events = 0

    for offset in range((today - start_day).days + 1):
        day = start_day + dt.timedelta(days=offset)
        if day.weekday() >= 5:  # no weekend working in the demo
            continue
        for employee, on_earlies in employees:
            if rng.random() < 0.06:  # the occasional day off
                continue
            shift_start = dt.time(6, 0) if on_earlies else dt.time(7, 30)
            shift_end = dt.time(14, 0) if on_earlies else dt.time(16, 0)

            # Arrive between 25 minutes early and 20 minutes late; odd minutes
            # on purpose so the 15-minute pay grid has something to do.
            in_local = dt.datetime.combine(day, shift_start) + dt.timedelta(
                minutes=rng.randint(-25, 20)
            )
            out_local = dt.datetime.combine(day, shift_end) + dt.timedelta(
                minutes=rng.randint(-10, 35)
            )

            db.session.add(
                AttendanceEvent(
                    employee_id=employee.id,
                    direction=DIRECTION_IN,
                    occurred_at=local_to_utc(in_local, tz),
                    method=METHOD_MANUAL,
                    note=DEMO_NOTE,
                )
            )
            events += 1
            if rng.random() < 0.03:  # forgot to clock out - must be flagged
                continue
            db.session.add(
                AttendanceEvent(
                    employee_id=employee.id,
                    direction=DIRECTION_OUT,
                    occurred_at=local_to_utc(out_local, tz),
                    method=METHOD_MANUAL,
                    note=DEMO_NOTE,
                )
            )
            events += 1

    db.session.commit()
    print(
        f"Added {len(employees)} demo employees (payroll refs DEMO01-DEMO{len(employees):02d}), "
        f"1 demo shift {DEMO_SHIFT_NAME!r} and {events} clocking events "
        f"covering {start_day.isoformat()} to {today.isoformat()}."
    )
    print("Remove it all later with: python scripts/seed_demo_data.py --remove")


def remove_demo_data() -> None:
    employees = db.session.scalars(
        select(Employee).where(Employee.payroll_ref.like("DEMO%"))
    ).all()
    for employee in employees:
        db.session.delete(employee)  # cascades to their attendance events

    shift = db.session.scalars(
        select(ShiftPattern).where(ShiftPattern.name == DEMO_SHIFT_NAME)
    ).first()
    if shift is not None:
        # Anyone real who was pointed at the demo shift falls back to the default.
        for employee in list(shift.employees):
            employee.shift_pattern_id = None
        db.session.delete(shift)

    db.session.commit()
    print(f"Removed {len(employees)} demo employees, their events, and the demo shift.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="Delete the demo data.")
    args = parser.parse_args()

    app = create_app("development")
    with app.app_context():
        if args.remove:
            remove_demo_data()
        else:
            add_demo_data()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
