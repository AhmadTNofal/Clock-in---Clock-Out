"""The kiosk: the touchscreen by the workshop door.

The page is deliberately unauthenticated - a shop-floor employee should walk up
and be clocked, nothing more. The endpoints that write attendance rows are
protected by the kiosk shared secret instead, which the page is rendered with.

Two clocking paths exist:

* **Hands-free** (the normal one) - the browser watches for somebody arriving,
  then calls ``/identify``, which recognises them but writes nothing. The screen
  shows who was seen and what is about to happen, counts down, and only then
  calls ``/commit``. That pause is the whole point: without it, walking past the
  camera two hours into a shift would clock you out.
* **Button press** - ``/scan`` recognises and records in one step, for the Scan,
  Clock in and Clock out buttons. A pressed button states intent, so it may
  override the hands-free interval.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..extensions import csrf, db
from ..models import DIRECTIONS, METHOD_AUTO, METHOD_FACE, Employee, utcnow
from ..security import rate_limit, require_kiosk_token
from ..services import attendance
from ..services.recognition import scan
from ..services.timesheet import get_timezone, to_local

bp = Blueprint("kiosk", __name__)

# Namespace for the short-lived tokens carrying an identification to /commit.
_CONFIRM_SALT = "kiosk-auto-confirm"


def _confirm_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_CONFIRM_SALT)


def _confirm_max_age() -> int:
    """How long an identification stays valid for committing.

    The countdown plus slack for a slow network. Deliberately short, so a token
    captured off the wire cannot be replayed later in the day.
    """
    return int(current_app.config["AUTO_CONFIRM_SECONDS"]) + 20


@bp.get("/")
def index():
    """Render the kiosk screen."""
    return render_template(
        "kiosk.html",
        kiosk_token=current_app.config["KIOSK_TOKEN"],
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        scan_frames=current_app.config["SCAN_FRAMES"],
        auto_mode=current_app.config["KIOSK_AUTO_MODE"],
        auto_confirm_seconds=current_app.config["AUTO_CONFIRM_SECONDS"],
        auto_poll_ms=current_app.config["AUTO_POLL_MS"],
        auto_presence_ms=current_app.config["AUTO_PRESENCE_MS"],
        auto_presence_threshold=current_app.config["AUTO_PRESENCE_THRESHOLD"],
        auto_scan_frames=current_app.config["AUTO_SCAN_FRAMES"],
        auto_frame_gap_ms=current_app.config["AUTO_FRAME_GAP_MS"],
        auto_min_interval_seconds=current_app.config["AUTO_MIN_INTERVAL_SECONDS"],
        capture_max_width=current_app.config["CAPTURE_MAX_WIDTH"],
        auto_require_departure=current_app.config["AUTO_REQUIRE_DEPARTURE"],
        auto_departure_ms=current_app.config["AUTO_DEPARTURE_MS"],
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


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _employee_payload(employee: Employee) -> dict:
    return {
        "id": employee.id,
        "name": employee.full_name,
        "first_name": employee.first_name,
        "payroll_ref": employee.payroll_ref,
        "department": employee.department,
    }


def _frames_from_request():
    """Pull and bound the frame list from the JSON body.

    Returns ``(frames, None)`` or ``(None, error_response)``.
    """
    payload = request.get_json(silent=True) or {}
    frames = payload.get("frames") or []
    if not isinstance(frames, list):
        return None, (
            jsonify(ok=False, code="bad_request", message="frames must be a list."),
            400,
        )
    limit = max(1, int(current_app.config["SCAN_FRAMES"]) + 2)
    return [f for f in frames if isinstance(f, (str, bytes))][:limit], None


def _identify(frames, *, automatic: bool):
    """Recognise the person in *frames*, writing nothing.

    Returns ``(employee, score, None)`` or ``(None, score, error_response)``.
    """
    outcome = scan(frames, automatic=automatic)
    score = outcome.score

    if not outcome.ok:
        if not automatic:
            # Hands-free polling would fill the log with "no face" lines, so
            # only a deliberate press is worth recording as a refusal.
            current_app.logger.info(
                "Kiosk scan refused: %s (best score %.3f)", outcome.code, score
            )
        return None, score, (
            jsonify(ok=False, code=outcome.code, message=outcome.message),
            200,
        )

    employee = db.session.get(Employee, outcome.employee_id)
    if employee is None or not employee.is_active:
        return None, score, (
            jsonify(
                ok=False,
                code="employee_inactive",
                message="Your record is not active. Please see the office.",
            ),
            200,
        )
    return employee, score, None


def _result_payload(employee: Employee, result, score: float) -> dict:
    tz = get_timezone(current_app.config["TIMEZONE"])
    current_app.logger.info(
        "%s %s (%.3f) recorded=%s method=%s",
        employee.payroll_ref,
        result.direction,
        score,
        result.recorded,
        result.event.method if result.event else "-",
    )
    return {
        "ok": True,
        "code": "recorded" if result.recorded else "duplicate",
        "message": result.message,
        "employee": _employee_payload(employee),
        "direction": result.direction,
        "recorded": result.recorded,
        "occurred_at": to_local(result.occurred_at, tz).strftime("%H:%M:%S"),
        "occurred_on": to_local(result.occurred_at, tz).strftime("%A %d %B %Y"),
        "confidence": round(score, 4),
        "next_direction": attendance.next_direction(employee.id),
    }


def _require_auto_mode():
    """Return an error response when hands-free clocking is switched off."""
    if current_app.config["KIOSK_AUTO_MODE"]:
        return None
    return (
        jsonify(ok=False, code="auto_disabled", message="Hands-free clocking is off."),
        403,
    )


# --------------------------------------------------------------------------
# Button-press clocking
# --------------------------------------------------------------------------
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
    frames, error = _frames_from_request()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_request", message="Unknown direction."), 400

    employee, score, refusal = _identify(frames, automatic=False)
    if refusal:
        return refusal

    result = attendance.record_clock(
        employee,
        direction=direction,
        confidence=score,
        method=METHOD_FACE,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        cooldown_seconds=current_app.config["CLOCK_COOLDOWN_SECONDS"],
    )
    return jsonify(**_result_payload(employee, result, score))


# --------------------------------------------------------------------------
# Hands-free clocking: identify, then commit
# --------------------------------------------------------------------------
@bp.post("/api/kiosk/identify")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_scan", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_identify():
    """Recognise whoever is at the kiosk **without recording anything**.

    Returns who was seen, what would be recorded, and a short-lived signed
    token. The signature is what stops a kiosk (or anything else holding the
    kiosk secret) clocking in an arbitrary employee: the employee id and the
    direction are decided here, server-side, and cannot be edited by the client
    without the application secret.
    """
    blocked = _require_auto_mode()
    if blocked:
        return blocked

    frames, error = _frames_from_request()
    if error:
        return error

    employee, score, refusal = _identify(frames, automatic=True)
    if refusal:
        return refusal

    # Nothing has been written, so work out what *would* happen.
    interval = int(current_app.config["AUTO_MIN_INTERVAL_SECONDS"])
    previous = attendance.last_event(employee.id)
    if previous is not None:
        age = (utcnow() - previous.occurred_at).total_seconds()
        if age < interval:
            # Clocked recently. Report the state and offer no token, so simply
            # standing near the kiosk cannot produce a second entry.
            tz = get_timezone(current_app.config["TIMEZONE"])
            verb = "in" if previous.direction == "in" else "out"
            return jsonify(
                ok=True,
                code="already_clocked",
                message=f"Already clocked {verb}, {employee.first_name}.",
                employee=_employee_payload(employee),
                direction=previous.direction,
                pending=False,
                occurred_at=to_local(previous.occurred_at, tz).strftime("%H:%M:%S"),
                seconds_until_next=max(0, int(interval - age)),
            )

    direction = attendance.next_direction(employee.id)
    token = _confirm_serializer().dumps(
        {
            "employee_id": employee.id,
            "direction": direction,
            "score": round(score, 4),
        }
    )
    return jsonify(
        ok=True,
        code="pending",
        message="",
        employee=_employee_payload(employee),
        direction=direction,
        pending=True,
        confirm_token=token,
        confirm_seconds=int(current_app.config["AUTO_CONFIRM_SECONDS"]),
        confidence=round(score, 4),
    )


@bp.post("/api/kiosk/commit")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_scan", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_commit():
    """Record the entry a previous /identify offered, given its signed token."""
    blocked = _require_auto_mode()
    if blocked:
        return blocked

    payload = request.get_json(silent=True) or {}
    token = payload.get("confirm_token")
    if not isinstance(token, str) or not token:
        return jsonify(ok=False, code="bad_request", message="Missing token."), 400

    try:
        data = _confirm_serializer().loads(token, max_age=_confirm_max_age())
    except SignatureExpired:
        return (
            jsonify(
                ok=False,
                code="confirm_expired",
                message="That took too long. Please face the camera again.",
            ),
            200,
        )
    except BadSignature:
        current_app.logger.warning("Rejected a kiosk commit carrying a bad signature.")
        return jsonify(ok=False, code="bad_token", message="Invalid confirmation."), 403

    direction = data.get("direction")
    if direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_token", message="Invalid confirmation."), 403

    employee = db.session.get(Employee, data.get("employee_id"))
    if employee is None or not employee.is_active:
        return (
            jsonify(
                ok=False,
                code="employee_inactive",
                message="Your record is not active. Please see the office.",
            ),
            200,
        )

    score = float(data.get("score") or 0.0)
    result = attendance.record_clock(
        employee,
        direction=direction,
        confidence=score,
        method=METHOD_AUTO,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        # A hands-free entry never overrides the interval, even though a
        # direction is supplied: being seen by a camera states no intent. This
        # also makes a replayed token harmless.
        cooldown_seconds=current_app.config["AUTO_MIN_INTERVAL_SECONDS"],
        automatic=True,
    )
    return jsonify(**_result_payload(employee, result, score))


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
