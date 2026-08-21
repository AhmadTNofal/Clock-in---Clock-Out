"""Database models.

Column types are deliberately generic (LargeBinary / String / DateTime) so the
same models run on MySQL in production and on SQLite in the test suite.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import bcrypt
from flask_login import UserMixin
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

# Direction / method values are stored as short strings rather than SQL ENUMs:
# adding a value later is then a code change, not a schema migration.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

METHOD_FACE = "face"
# Recorded hands-free, with no button press. Kept distinct from METHOD_FACE so a
# payroll query can tell a deliberate scan from an automatic one.
METHOD_AUTO = "auto"
METHOD_MANUAL = "manual"


def utcnow() -> dt.datetime:
    """Timezone-naive UTC timestamp (MySQL DATETIME stores no offset)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class ShiftPattern(db.Model):
    """A paid time band, e.g. 07:30-16:00 with a 30-minute unpaid lunch.

    Times are *local* wall-clock times (the timezone the site runs in), not UTC:
    a 07:30 start means 07:30 on the shop floor all year round, either side of
    the BST/GMT change. An end time at or before the start time means the shift
    runs overnight into the next day.
    """

    __tablename__ = "shift_pattern"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    unpaid_break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    employees: Mapped[list["Employee"]] = relationship(back_populates="shift_pattern")

    @property
    def crosses_midnight(self) -> bool:
        return self.end_time <= self.start_time

    @property
    def label(self) -> str:
        return (
            f"{self.name} ({self.start_time.strftime('%H:%M')}"
            f"–{self.end_time.strftime('%H:%M')})"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ShiftPattern {self.name} {self.start_time}-{self.end_time}>"


class Employee(db.Model):
    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payroll_ref: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    department: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(190))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # NULL means "use the default shift pattern", so new starters need no setup.
    shift_pattern_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_pattern.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    templates: Mapped[list["FaceTemplate"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list["AttendanceEvent"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="select"
    )
    shift_pattern: Mapped[ShiftPattern | None] = relationship(back_populates="employees")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_enrolled(self) -> bool:
        return len(self.templates) > 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Employee {self.payroll_ref} {self.full_name}>"


class FaceTemplate(db.Model):
    """One enrolled face embedding: 128 float32 values, L2-normalised."""

    __tablename__ = "face_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False, index=True
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary(2048), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=128)
    sharpness: Mapped[float | None] = mapped_column(Float)
    face_pixels: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )

    employee: Mapped[Employee] = relationship(back_populates="templates")

    def as_vector(self) -> "np.ndarray":
        """Return the stored embedding as a 1-D float32 numpy array."""
        import numpy as np

        return np.frombuffer(self.embedding, dtype=np.float32)

    @staticmethod
    def pack(vector: "np.ndarray") -> bytes:
        """Serialise a numpy embedding for storage."""
        import numpy as np

        return np.ascontiguousarray(vector, dtype=np.float32).tobytes()


class AttendanceEvent(db.Model):
    """An append-only clock-in / clock-out log entry.

    Nothing is ever overwritten: an incorrect entry is voided (is_voided) and a
    corrected one added, so the audit trail stays intact for payroll queries.
    """

    __tablename__ = "attendance_event"
    __table_args__ = (Index("ix_attendance_employee_time", "employee_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False, default=METHOD_FACE)
    confidence: Mapped[float | None] = mapped_column(Float)
    device_label: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    is_voided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )

    employee: Mapped[Employee] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttendanceEvent {self.employee_id} {self.direction} {self.occurred_at}>"


class AdminUser(UserMixin, db.Model):
    """A back-office login. Kiosk users never authenticate as one of these."""

    __tablename__ = "admin_user"
    __table_args__ = (UniqueConstraint("username", name="uq_admin_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128))
    is_active_flag: Mapped[bool] = mapped_column(
        "is_active", Boolean, nullable=False, default=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    # --- password handling -------------------------------------------------
    def set_password(self, password: str) -> None:
        if not password or len(password) < 10:
            raise ValueError("Password must be at least 10 characters long.")
        # bcrypt silently truncates beyond 72 bytes; reject rather than mislead.
        if len(password.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer.")
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("ascii")

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("ascii"))
        except (ValueError, TypeError):
            return False

    # Flask-Login reads is_active to block disabled accounts.
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return bool(self.is_active_flag)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AdminUser {self.username}>"
