"""The kiosk: the touchscreen by the workshop door.

The page is deliberately unauthenticated - a shop-floor employee should walk up
and scan, nothing more. The endpoint that writes attendance rows is protected by
the kiosk shared secret instead, which the page is rendered with.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from ..extensions import csrf, db
from ..models import DIRECTIONS, METHOD_FACE, Employee
from ..security import rate_limit, require_kiosk_token
from ..services import attendance
from ..services.recognition import scan
from ..services.timesheet import get_timezone, to_local

bp = Blueprint("kiosk", __name__)


@bp.get("/")
def index():
    """Render the kiosk screen."""
    return render_template(
        "kiosk.html",
        kiosk_token=current_app.config["KIOSK_TOKEN"],
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        scan_frames=current_app.config["SCAN_FRAMES"],
    )


@bp.get("/healthz")
def healthz():
    """Cheap probe for a scheduled task or monitoring script.

    Reports the two things that actually stop clocking working: the database
    being unreachable and the face models being absent.
    """
    from sqlalchemy import text

    try:
        db.session.execute(text("SELECT 1"))
        database_ok = True
    except Exception:  # noqa: BLE001 - report the state, do not raise
        database_ok = False

    models_ok = (
        current_app.config["FACE_DETECTOR_MODEL"].is_file()
        and current_app.config["FACE_RECOGNISER_MODEL"].is_file()
    )

    healthy = database_ok and models_ok
    return (
        jsonify(ok=healthy, service="clocking", database=database_ok, models=models_ok),
        200 if healthy else 503,
    )


# CSRF is exempt because the caller is the kiosk JavaScript authenticating with
# the shared secret in a header, not a browser form carrying a session cookie.
@bp.post("/api/kiosk/scan")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_scan", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_scan():
    """Identify the person in the posted frames and record a clock event.

    Expects JSON: ``{"frames": ["data:image/jpeg;base64,..."], "direction": null}``
    where *direction* is optional and forces "in" or "out" if the employee used
    the explicit buttons rather than the automatic alternation.
    """
    payload = request.get_json(silent=True) or {}
    frames = payload.get("frames") or []
    if not isinstance(frames, list):
        return jsonify(ok=False, code="bad_request", message="frames must be a list."), 400

    max_frames = max(1, int(current_app.config["SCAN_FRAMES"]) + 2)
    frames = [f for f in frames if isinstance(f, (str, bytes))][:max_frames]

    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_request", message="Unknown direction."), 400

    outcome = scan(frames)
    if not outcome.ok:
        current_app.logger.info(
            "Kiosk scan refused: %s (best score %.3f)", outcome.code, outcome.score
        )
        return jsonify(ok=False, code=outcome.code, message=outcome.message), 200

    employee = db.session.get(Employee, outcome.employee_id)
    if employee is None or not employee.is_active:
        return (
            jsonify(
                ok=False,
                code="employee_inactive",
                message="Your record is not active. Please see the office.",
            ),
            200,
        )

    result = attendance.record_clock(
        employee,
        direction=direction,
        confidence=outcome.score,
        method=METHOD_FACE,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        cooldown_seconds=current_app.config["CLOCK_COOLDOWN_SECONDS"],
    )

    tz = get_timezone(current_app.config["TIMEZONE"])
    current_app.logger.info(
        "%s %s (%.3f) recorded=%s",
        employee.payroll_ref,
        result.direction,
        outcome.score,
        result.recorded,
    )

    return jsonify(
        ok=True,
        code="recorded" if result.recorded else "duplicate",
        message=result.message,
        employee={
            "id": employee.id,
            "name": employee.full_name,
            "first_name": employee.first_name,
            "payroll_ref": employee.payroll_ref,
            "department": employee.department,
        },
        direction=result.direction,
        recorded=result.recorded,
        occurred_at=to_local(result.occurred_at, tz).strftime("%H:%M:%S"),
        occurred_on=to_local(result.occurred_at, tz).strftime("%A %d %B %Y"),
        confidence=round(outcome.score, 4),
        next_direction=attendance.next_direction(employee.id),
    )


@bp.get("/api/kiosk/onsite")
@require_kiosk_token
def api_onsite():
    """Who is currently on site - drives the counter on the kiosk screen."""
    people = attendance.currently_on_site()
    return jsonify(
        ok=True,
        count=len(people),
        names=[person.full_name for person in people],
    )
