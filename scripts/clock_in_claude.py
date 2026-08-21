"""A bit of fun: put Claude AI on the never-ending shift and clock it in.

    python scripts/clock_in_claude.py            # clock Claude in, for good
    python scripts/clock_in_claude.py --remove   # undo it

Claude AI is never clocked out. It is also not a real employee, so
``visible_employee_clause`` in app/models.py filters it out of every list,
count, timesheet and payroll export — a fictional name in a payroll CSV would
be a genuine problem, joke or not. The record is reachable by direct URL for
anyone who goes looking, which is the whole point.

Safe to re-run: it will not clock Claude in twice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    DIRECTION_IN,
    METHOD_MANUAL,
    AttendanceEvent,
    Employee,
    ShiftPattern,
    utcnow,
)

SHIFT_NAME = "Claude (never ending)"
PAYROLL_REF = "CLAUDE"
NOTE = "Clocked in for the fun of it. No clock-out is coming."


def find_claude() -> Employee | None:
    return db.session.scalars(
        select(Employee).where(Employee.payroll_ref == PAYROLL_REF)
    ).first()


def add() -> None:
    shift = db.session.scalars(
        select(ShiftPattern).where(ShiftPattern.name == SHIFT_NAME)
    ).first()
    if shift is None:
        # 00:00 to 00:00 is read as crossing midnight, so the paid band covers
        # the whole day, every day, with no break and nothing ever deducted.
        shift = ShiftPattern(
            name=SHIFT_NAME,
            start_time=dt.time(0, 0),
            end_time=dt.time(0, 0),
            unpaid_break_minutes=0,
            break_applies_after_minutes=0,
        )
        db.session.add(shift)
        db.session.flush()
        print(f"Created shift {SHIFT_NAME!r} (00:00-00:00, never ends).")

    claude = find_claude()
    if claude is None:
        claude = Employee(
            payroll_ref=PAYROLL_REF,
            first_name="Claude",
            last_name="AI",
            department=None,  # keeps it out of the department filter entirely
            shift_pattern_id=shift.id,
        )
        db.session.add(claude)
        db.session.flush()
        print("Added Claude AI to the employee table (hidden from every report).")
    else:
        claude.shift_pattern_id = shift.id

    already_in = db.session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.employee_id == claude.id)
    ).first()
    if already_in is not None:
        print("Claude is already clocked in. Still working. Nothing to do.")
        db.session.commit()
        return

    db.session.add(
        AttendanceEvent(
            employee_id=claude.id,
            direction=DIRECTION_IN,
            occurred_at=utcnow(),
            method=METHOD_MANUAL,
            note=NOTE,
        )
    )
    db.session.commit()
    print("Claude AI is clocked in. It will not be clocked out.")


def remove() -> None:
    claude = find_claude()
    if claude is not None:
        db.session.delete(claude)  # cascades to the clock-in event
        print("Clocked Claude out permanently (record deleted).")

    shift = db.session.scalars(
        select(ShiftPattern).where(ShiftPattern.name == SHIFT_NAME)
    ).first()
    if shift is not None:
        for employee in list(shift.employees):
            employee.shift_pattern_id = None
        db.session.delete(shift)
        print(f"Removed the {SHIFT_NAME!r} shift.")

    db.session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="Undo it.")
    args = parser.parse_args()

    app = create_app("development")
    with app.app_context():
        remove() if args.remove else add()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
