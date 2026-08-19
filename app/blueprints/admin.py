"""Back office: employees, enrolment, timesheets and corrections."""

from __future__ import annotations

import datetime as dt

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import BooleanField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

from ..extensions import db
from ..models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    DIRECTIONS,
    METHOD_MANUAL,
    AttendanceEvent,
    Employee,
)
from ..services import attendance
from ..services.enrolment import enrol_employee, remove_enrolment
from ..services.recognition import get_index
from ..services.timesheet import (
    build_timesheet,
    get_timezone,
    summarise,
    to_csv,
    to_local,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
@login_required
def require_login():
    """Every route in this blueprint needs a signed-in administrator."""
    return None


# --------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------
class EmployeeForm(FlaskForm):
    payroll_ref = StringField("Payroll reference", validators=[DataRequired(), Length(max=32)])
    first_name = StringField("First name", validators=[DataRequired(), Length(max=64)])
    last_name = StringField("Surname", validators=[DataRequired(), Length(max=64)])
    department = StringField("Department", validators=[Optional(), Length(max=64)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=190)])
    is_active = BooleanField("Active", default=True)


class ManualEventForm(FlaskForm):
    employee_id = SelectField("Employee", coerce=int, validators=[DataRequired()])
    direction = SelectField(
        "Direction",
        choices=[(DIRECTION_IN, "Clock in"), (DIRECTION_OUT, "Clock out")],
        validators=[DataRequired()],
    )
    occurred_at = StringField(
        "Date and time (local)", validators=[DataRequired(), Length(max=32)]
    )
    note = TextAreaField("Reason", validators=[DataRequired(), Length(max=500)])


class VoidForm(FlaskForm):
    """Just a CSRF-protected submit for voiding one event."""

    note = StringField("Reason", validators=[DataRequired(), Length(max=200)])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _tz():
    return get_timezone(current_app.config["TIMEZONE"])


def _parse_date(raw: str | None, default: dt.date) -> dt.date:
    if not raw:
        return default
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return default


def _local_to_utc(raw: str) -> dt.datetime:
    """Parse an operator-entered local date/time into a naive UTC timestamp."""
    text = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            naive = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (
            naive.replace(tzinfo=_tz())
            .astimezone(dt.timezone.utc)
            .replace(tzinfo=None)
        )
    raise ValueError("Use the format YYYY-MM-DD HH:MM")


def _employee_choices() -> list[tuple[int, str]]:
    employees = db.session.scalars(
        select(Employee).order_by(Employee.last_name, Employee.first_name)
    ).all()
    return [(e.id, f"{e.last_name}, {e.first_name} ({e.payroll_ref})") for e in employees]


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@bp.get("/")
def dashboard():
    tz = _tz()
    today = dt.datetime.now(tz).date()
    on_site = attendance.currently_on_site()

    recent = db.session.scalars(
        select(AttendanceEvent)
        .where(AttendanceEvent.is_voided.is_(False))
        .order_by(AttendanceEvent.occurred_at.desc())
        .limit(15)
    ).all()

    employees = db.session.scalars(select(Employee)).all()
    index = get_index()

    return render_template(
        "admin/dashboard.html",
        today=today,
        on_site=on_site,
        recent=recent,
        employee_count=len(employees),
        active_count=sum(1 for e in employees if e.is_active),
        enrolled_count=sum(1 for e in employees if e.is_enrolled),
        index_size=index.size,
    )


# --------------------------------------------------------------------------
# Employees
# --------------------------------------------------------------------------
@bp.get("/employees")
def employees():
    query = (request.args.get("q") or "").strip()
    stmt = select(Employee).order_by(Employee.last_name, Employee.first_name)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            Employee.first_name.ilike(like)
            | Employee.last_name.ilike(like)
            | Employee.payroll_ref.ilike(like)
            | Employee.department.ilike(like)
        )
    return render_template(
        "admin/employees.html", employees=db.session.scalars(stmt).all(), query=query
    )


@bp.route("/employees/new", methods=["GET", "POST"])
def employee_new():
    form = EmployeeForm()
    if form.validate_on_submit():
        clash = db.session.scalars(
            select(Employee).where(Employee.payroll_ref == form.payroll_ref.data)
        ).first()
        if clash is not None:
            flash(f"Payroll reference {form.payroll_ref.data} is already in use.", "error")
            return render_template("admin/employee_form.html", form=form, employee=None)

        employee = Employee(
            payroll_ref=(form.payroll_ref.data or "").strip(),
            first_name=(form.first_name.data or "").strip(),
            last_name=(form.last_name.data or "").strip(),
            department=(form.department.data or "").strip() or None,
            email=(form.email.data or "").strip() or None,
            is_active=bool(form.is_active.data),
        )
        db.session.add(employee)
        db.session.commit()
        flash(f"Added {employee.full_name}. Now enrol their face.", "success")
        return redirect(url_for("admin.employee_enrol", employee_id=employee.id))

    return render_template("admin/employee_form.html", form=form, employee=None)


@bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def employee_edit(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    form = EmployeeForm(obj=employee)
    if form.validate_on_submit():
        clash = db.session.scalars(
            select(Employee).where(
                Employee.payroll_ref == form.payroll_ref.data, Employee.id != employee.id
            )
        ).first()
        if clash is not None:
            flash(f"Payroll reference {form.payroll_ref.data} is already in use.", "error")
            return render_template("admin/employee_form.html", form=form, employee=employee)

        employee.payroll_ref = (form.payroll_ref.data or "").strip()
        employee.first_name = (form.first_name.data or "").strip()
        employee.last_name = (form.last_name.data or "").strip()
        employee.department = (form.department.data or "").strip() or None
        employee.email = (form.email.data or "").strip() or None
        employee.is_active = bool(form.is_active.data)
        db.session.commit()
        flash(f"Updated {employee.full_name}.", "success")
        return redirect(url_for("admin.employees"))

    return render_template("admin/employee_form.html", form=form, employee=employee)


@bp.get("/employees/<int:employee_id>")
def employee_detail(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    events = db.session.scalars(
        select(AttendanceEvent)
        .where(AttendanceEvent.employee_id == employee.id)
        .order_by(AttendanceEvent.occurred_at.desc())
        .limit(50)
    ).all()
    return render_template(
        "admin/employee_detail.html",
        employee=employee,
        events=events,
        void_form=VoidForm(),
        clocked_in=attendance.is_clocked_in(employee.id),
    )


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------
@bp.get("/employees/<int:employee_id>/enrol")
def employee_enrol(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    return render_template(
        "admin/enrol.html",
        employee=employee,
        min_samples=current_app.config["ENROL_MIN_SAMPLES"],
        max_samples=current_app.config["ENROL_MAX_SAMPLES"],
    )


@bp.post("/employees/<int:employee_id>/enrol")
def employee_enrol_submit(employee_id: int):
    """Receive captured samples from the enrolment page (JSON, session-authenticated)."""
    employee = db.get_or_404(Employee, employee_id)
    payload = request.get_json(silent=True) or {}
    frames = payload.get("frames") or []
    if not isinstance(frames, list) or not frames:
        return jsonify(ok=False, code="no_frames", message="No samples were captured."), 400

    outcome = enrol_employee(
        employee,
        [f for f in frames if isinstance(f, str)][: current_app.config["ENROL_MAX_SAMPLES"] + 4],
        admin_id=current_user.id,
        replace_existing=bool(payload.get("replace")),
    )
    return jsonify(
        ok=outcome.ok,
        code=outcome.code,
        message=outcome.message,
        added=outcome.added,
        rejected=outcome.rejected,
        template_count=len(employee.templates),
    )


@bp.post("/employees/<int:employee_id>/enrol/clear")
def employee_enrol_clear(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    removed = remove_enrolment(employee)
    flash(f"Removed {removed} face sample(s) for {employee.full_name}.", "success")
    return redirect(url_for("admin.employee_detail", employee_id=employee.id))


# --------------------------------------------------------------------------
# Timesheets
# --------------------------------------------------------------------------
def _timesheet_args():
    tz = _tz()
    today = dt.datetime.now(tz).date()
    default_start = today - dt.timedelta(days=today.weekday())  # Monday this week
    start = _parse_date(request.args.get("start"), default_start)
    end = _parse_date(request.args.get("end"), today)
    if end < start:
        start, end = end, start
    raw_employee = request.args.get("employee_id")
    employee_id = int(raw_employee) if raw_employee and raw_employee.isdigit() else None
    return start, end, employee_id, tz


@bp.get("/timesheets")
def timesheets():
    start, end, employee_id, tz = _timesheet_args()
    shifts = build_timesheet(start, end, tz, employee_id=employee_id)
    return render_template(
        "admin/timesheets.html",
        shifts=shifts,
        totals=summarise(shifts),
        start=start,
        end=end,
        employee_id=employee_id,
        employees=db.session.scalars(
            select(Employee).order_by(Employee.last_name, Employee.first_name)
        ).all(),
    )


@bp.get("/timesheets.csv")
def timesheets_csv():
    start, end, employee_id, tz = _timesheet_args()
    shifts = build_timesheet(start, end, tz, employee_id=employee_id)
    filename = f"timesheet_{start.isoformat()}_to_{end.isoformat()}.csv"
    return Response(
        to_csv(shifts),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Corrections
# --------------------------------------------------------------------------
@bp.route("/events/manual", methods=["GET", "POST"])
def event_manual():
    form = ManualEventForm()
    form.employee_id.choices = _employee_choices()

    if form.validate_on_submit():
        employee = db.session.get(Employee, form.employee_id.data)
        if employee is None:
            flash("That employee no longer exists.", "error")
            return render_template("admin/event_manual.html", form=form)
        try:
            occurred_at = _local_to_utc(form.occurred_at.data or "")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("admin/event_manual.html", form=form)

        result = attendance.record_clock(
            employee,
            direction=form.direction.data,
            method=METHOD_MANUAL,
            note=f"Manual entry by {current_user.username}: {form.note.data}",
            created_by_id=current_user.id,
            occurred_at=occurred_at,
            cooldown_seconds=0,  # an operator entering a correction means it
        )
        flash(
            f"Recorded manual {result.direction} for {employee.full_name} "
            f"at {to_local(occurred_at, _tz()).strftime('%d/%m/%Y %H:%M')}.",
            "success",
        )
        return redirect(url_for("admin.employee_detail", employee_id=employee.id))

    if not form.occurred_at.data:
        form.occurred_at.data = dt.datetime.now(_tz()).strftime("%Y-%m-%d %H:%M")
    return render_template("admin/event_manual.html", form=form)


@bp.post("/events/<int:event_id>/void")
def event_void(event_id: int):
    event = db.get_or_404(AttendanceEvent, event_id)
    form = VoidForm()
    if not form.validate_on_submit():
        flash("A reason is required to void an entry.", "error")
        return redirect(url_for("admin.employee_detail", employee_id=event.employee_id))

    attendance.void_event(event, admin_id=current_user.id, reason=form.note.data or "")
    flash("Entry voided. The original row is kept for the audit trail.", "success")
    return redirect(url_for("admin.employee_detail", employee_id=event.employee_id))


# --------------------------------------------------------------------------
# Camera diagnostics
# --------------------------------------------------------------------------
@bp.get("/camera-check")
def camera_check():
    """Measure what the kiosk camera actually produces.

    Face size, sharpness and motion vary enormously between cameras and
    lighting, so rather than guessing at thresholds this page reports the real
    numbers from a live capture. Set FACE_MIN_PIXELS and FACE_MIN_SHARPNESS in
    .env comfortably below what a cooperative employee measures here.
    """
    return render_template(
        "admin/camera_check.html",
        thresholds={
            "min_pixels": current_app.config["FACE_MIN_PIXELS"],
            "min_sharpness": current_app.config["FACE_MIN_SHARPNESS"],
            "match_threshold": current_app.config["FACE_MATCH_THRESHOLD"],
            "match_margin": current_app.config["FACE_MATCH_MARGIN"],
            "min_motion": current_app.config["LIVENESS_MIN_MOTION"],
        },
    )


@bp.post("/camera-check")
def camera_check_submit():
    """Report measurements for posted frames without recording anything."""
    from ..face.engine import FaceError, ModelsMissing, decode_image
    from ..face.liveness import frame_consistency, frame_motion
    from ..services.recognition import get_engine

    payload = request.get_json(silent=True) or {}
    frames = [f for f in (payload.get("frames") or []) if isinstance(f, str)][:5]
    if not frames:
        return jsonify(ok=False, message="No frames were captured."), 400

    try:
        engine = get_engine()
    except ModelsMissing as exc:
        return jsonify(ok=False, message=str(exc)), 200
    observations = []
    problems = []
    for position, frame in enumerate(frames, start=1):
        try:
            # Quality gates are off here: the point is to measure, not to judge.
            observations.append(engine.observe(decode_image(frame), check_quality=False))
        except FaceError as exc:
            problems.append(f"Frame {position}: {exc.message}")

    if not observations:
        return jsonify(ok=False, message="No face was measured.", problems=problems)

    index = get_index()
    best = (
        index.match(
            observations[0].embedding,
            threshold=current_app.config["FACE_MATCH_THRESHOLD"],
            margin=current_app.config["FACE_MATCH_MARGIN"],
        )
        if index.size
        else None
    )
    matched = db.session.get(Employee, best.employee_id) if best and best.accepted else None

    return jsonify(
        ok=True,
        frames_measured=len(observations),
        problems=problems,
        face_pixels=[obs.width for obs in observations],
        sharpness=[round(obs.sharpness, 1) for obs in observations],
        detector_score=[round(obs.score, 3) for obs in observations],
        motion=round(frame_motion(observations), 2),
        consistency=round(frame_consistency(observations), 4),
        best_match={
            "employee": matched.full_name if matched else None,
            "score": round(best.score, 4) if best else None,
            "reason": best.reason if best else "no_templates",
        },
    )
