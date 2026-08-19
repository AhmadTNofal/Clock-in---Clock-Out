"""Hands-free (automatic) clocking.

The two properties that matter most here are safety ones:

* walking past the camera mid-shift must not clock you out;
* the kiosk must not be able to clock in an employee of its own choosing - the
  identification is decided server-side and signed.
"""

from __future__ import annotations

import datetime as dt
import threading

import numpy as np
import pytest

from app.face.engine import (
    EngineSettings,
    FaceEngine,
    MultipleFacesFound,
)
from app.models import METHOD_AUTO, AttendanceEvent, Employee, utcnow
from app.services import attendance

from .conftest import add_template, make_employee, nudge, unit_vector
from .test_routes import _stub_engine

TOKEN = "test-kiosk-token"


def _identify(client, frames=("a", "b")):
    return client.post(
        "/api/kiosk/identify",
        json={"frames": list(frames)},
        headers={"X-Kiosk-Token": TOKEN},
    ).get_json()


def _commit(client, token):
    return client.post(
        "/api/kiosk/commit",
        json={"confirm_token": token},
        headers={"X-Kiosk-Token": TOKEN},
    )


@pytest.fixture
def enrolled(db, monkeypatch):
    """An enrolled employee whose face the stubbed engine always returns."""
    employee = make_employee(db)
    face = unit_vector(1)
    add_template(db, employee, face)
    _stub_engine(monkeypatch, [nudge(face, 0.2), nudge(face, 0.22)])
    return employee


# --------------------------------------------------------------------------
# identify writes nothing
# --------------------------------------------------------------------------
def test_identify_recognises_without_recording(client, db, enrolled):
    payload = _identify(client)

    assert payload["ok"] is True
    assert payload["code"] == "pending"
    assert payload["pending"] is True
    assert payload["employee"]["payroll_ref"] == "E001"
    assert payload["direction"] == "in"
    assert payload["confirm_token"]
    # The whole point: nothing in the database yet.
    assert db.session.query(AttendanceEvent).count() == 0


def test_commit_records_the_entry(client, db, enrolled):
    token = _identify(client)["confirm_token"]
    response = _commit(client, token)
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["recorded"] is True
    assert payload["direction"] == "in"

    event = db.session.query(AttendanceEvent).one()
    assert event.direction == "in"
    # Recorded as automatic, so payroll can tell it from a deliberate scan.
    assert event.method == METHOD_AUTO


def test_cancelling_means_never_committing(client, db, enrolled):
    """Cancelling on screen simply drops the token - there is nothing to undo."""
    payload = _identify(client)
    assert payload["confirm_token"]
    # The kiosk throws the token away; no /commit call is made.
    assert db.session.query(AttendanceEvent).count() == 0


# --------------------------------------------------------------------------
# The token is what makes this safe
# --------------------------------------------------------------------------
def test_a_tampered_token_is_refused(client, db, enrolled):
    token = _identify(client)["confirm_token"]
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")

    response = _commit(client, tampered)
    assert response.status_code == 403
    assert response.get_json()["code"] == "bad_token"
    assert db.session.query(AttendanceEvent).count() == 0


def test_a_token_signed_with_another_secret_is_refused(client, db, enrolled, app):
    """A kiosk cannot mint its own identifications without the app secret."""
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer("not-the-app-secret", salt="kiosk-auto-confirm").dumps(
        {"employee_id": enrolled.id, "direction": "in", "score": 0.99}
    )
    response = _commit(client, forged)
    assert response.status_code == 403
    assert db.session.query(AttendanceEvent).count() == 0


def test_a_token_naming_a_different_employee_cannot_be_forged(client, db, enrolled):
    """The client never gets to choose who is clocked in."""
    other = make_employee(db, ref="E002", first="Bob", last="Bright")
    # The only way to name Bob would be to re-sign the payload.
    token = _identify(client)["confirm_token"]
    _commit(client, token)

    events = db.session.query(AttendanceEvent).all()
    assert len(events) == 1
    assert events[0].employee_id == enrolled.id
    assert events[0].employee_id != other.id


def test_an_expired_token_asks_them_to_try_again(client, db, enrolled, app, monkeypatch):
    token = _identify(client)["confirm_token"]
    # Pretend the confirmation window has long passed.
    monkeypatch.setattr("app.blueprints.kiosk._confirm_max_age", lambda: -1)

    response = _commit(client, token)
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["code"] == "confirm_expired"
    assert db.session.query(AttendanceEvent).count() == 0


def test_replaying_a_token_does_not_double_record(client, db, enrolled):
    """Even a captured token is harmless: the interval suppresses the second."""
    token = _identify(client)["confirm_token"]
    first = _commit(client, token).get_json()
    second = _commit(client, token).get_json()

    assert first["recorded"] is True
    assert second["recorded"] is False
    assert db.session.query(AttendanceEvent).count() == 1


def test_commit_needs_a_token(client, enrolled):
    response = client.post(
        "/api/kiosk/commit", json={}, headers={"X-Kiosk-Token": TOKEN}
    )
    assert response.status_code == 400


def test_identify_and_commit_need_the_kiosk_token(client, enrolled):
    assert client.post("/api/kiosk/identify", json={"frames": ["a"]}).status_code == 403
    assert client.post("/api/kiosk/commit", json={"confirm_token": "x"}).status_code == 403


# --------------------------------------------------------------------------
# Walking past must not clock you out
# --------------------------------------------------------------------------
def test_walking_past_mid_shift_does_not_clock_you_out(client, db, enrolled, app):
    """The headline safety property of hands-free clocking.

    Somebody clocked in five minutes ago crosses the camera's view again. The
    naive behaviour - alternate the direction - would clock them out and lose
    their whole shift. Instead the kiosk reports their state and offers no token.
    """
    app.config["AUTO_MIN_INTERVAL_SECONDS"] = 600
    attendance.record_clock(
        enrolled,
        direction="in",
        occurred_at=utcnow() - dt.timedelta(minutes=5),
        cooldown_seconds=0,
    )

    payload = _identify(client)

    assert payload["ok"] is True
    assert payload["code"] == "already_clocked"
    assert payload["pending"] is False
    assert "confirm_token" not in payload
    assert payload["direction"] == "in"
    assert payload["seconds_until_next"] > 0
    # Still exactly the one clock-in.
    assert db.session.query(AttendanceEvent).count() == 1
    assert attendance.is_clocked_in(enrolled.id)


def test_identify_offers_a_clock_out_once_the_interval_has_passed(client, db, enrolled, app):
    app.config["AUTO_MIN_INTERVAL_SECONDS"] = 600
    attendance.record_clock(
        enrolled,
        direction="in",
        occurred_at=utcnow() - dt.timedelta(minutes=20),
        cooldown_seconds=0,
    )

    payload = _identify(client)
    assert payload["code"] == "pending"
    assert payload["direction"] == "out"

    _commit(client, payload["confirm_token"])
    assert not attendance.is_clocked_in(enrolled.id)


def test_a_button_press_overrides_the_hands_free_interval(client, db, enrolled, app):
    """Somebody who really is leaving straight away can still press Clock out."""
    app.config["AUTO_MIN_INTERVAL_SECONDS"] = 600
    app.config["CLOCK_COOLDOWN_SECONDS"] = 90
    _commit(client, _identify(client)["confirm_token"])
    assert attendance.is_clocked_in(enrolled.id)

    payload = client.post(
        "/api/kiosk/scan",
        json={"frames": ["a", "b"], "direction": "out"},
        headers={"X-Kiosk-Token": TOKEN},
    ).get_json()

    assert payload["recorded"] is True
    assert payload["direction"] == "out"
    assert not attendance.is_clocked_in(enrolled.id)


def test_automatic_entries_never_override_the_interval_even_with_a_direction(db):
    """Guards the rule directly: automatic=True ignores a stated direction."""
    employee = make_employee(db)
    attendance.record_clock(employee, direction="in", cooldown_seconds=0)

    result = attendance.record_clock(
        employee, direction="out", cooldown_seconds=600, automatic=True
    )
    assert not result.recorded
    assert db.session.query(AttendanceEvent).count() == 1


# --------------------------------------------------------------------------
# Switching hands-free off
# --------------------------------------------------------------------------
def test_identify_is_refused_when_auto_mode_is_off(client, app, enrolled):
    app.config["KIOSK_AUTO_MODE"] = False
    response = client.post(
        "/api/kiosk/identify", json={"frames": ["a"]}, headers={"X-Kiosk-Token": TOKEN}
    )
    assert response.status_code == 403
    assert response.get_json()["code"] == "auto_disabled"


def test_commit_is_refused_when_auto_mode_is_off(client, app, enrolled):
    token = _identify(client)["confirm_token"]
    app.config["KIOSK_AUTO_MODE"] = False
    assert _commit(client, token).status_code == 403


def test_buttons_still_work_when_auto_mode_is_off(client, db, app, enrolled):
    app.config["KIOSK_AUTO_MODE"] = False
    payload = client.post(
        "/api/kiosk/scan", json={"frames": ["a", "b"]}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()
    assert payload["recorded"] is True


def test_kiosk_page_carries_the_auto_settings(client, app):
    app.config["KIOSK_AUTO_MODE"] = True
    body = client.get("/").data.decode("utf-8")
    assert "identifyUrl" in body
    assert "commitUrl" in body
    assert "autoMode: true" in body
    assert "cancel-btn" in body


# --------------------------------------------------------------------------
# Bystanders: the "dominant face" rule
# --------------------------------------------------------------------------
def _row(width: float, score: float = 0.95) -> np.ndarray:
    """A synthetic YuNet row: x, y, w, h, five landmarks, score."""
    row = np.zeros(15, dtype=np.float32)
    row[0], row[1], row[2], row[3] = 10.0, 10.0, width, width * 1.25
    row[-1] = score
    return row


def _engine_with_rows(rows):
    """A FaceEngine with detection and embedding stubbed - no models needed."""
    engine = FaceEngine.__new__(FaceEngine)
    engine.settings = EngineSettings(
        detector_model="x", recogniser_model="y", min_face_pixels=0, min_sharpness=0.0
    )
    engine._lock = threading.Lock()
    engine._detect_rows = lambda image: np.vstack(rows) if rows else np.empty((0, 15))
    engine._embed_row = lambda image, row: (
        unit_vector(int(row[2])),  # embedding keyed on width, so we can tell them apart
        500.0,
        np.zeros((112, 112, 3), dtype=np.uint8),
    )
    return engine


def _frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def test_a_bystander_further_away_does_not_block_recognition():
    """On a shop floor somebody is usually walking past behind the kiosk user."""
    engine = _engine_with_rows([_row(200), _row(60)])

    observation = engine.observe(_frame(), dominant_ratio=1.35)
    # The nearest (widest) face wins.
    assert observation.box[2] == 200


def test_two_people_equally_close_are_refused():
    """If nobody is clearly at the kiosk, guessing could clock in the wrong person."""
    engine = _engine_with_rows([_row(200), _row(190)])

    with pytest.raises(MultipleFacesFound, match="one at a time"):
        engine.observe(_frame(), dominant_ratio=1.35)


def test_the_dominance_rule_is_off_for_button_presses():
    """Press-to-scan keeps the stricter one-person-only behaviour."""
    engine = _engine_with_rows([_row(200), _row(60)])

    with pytest.raises(MultipleFacesFound):
        engine.observe(_frame())


def test_a_single_face_is_unaffected_by_the_rule():
    engine = _engine_with_rows([_row(150)])
    assert engine.observe(_frame(), dominant_ratio=1.35).box[2] == 150


def test_the_ratio_boundary_behaves():
    # 200 vs 100 is a ratio of 2.0, comfortably dominant.
    assert _engine_with_rows([_row(200), _row(100)]).observe(
        _frame(), dominant_ratio=1.35
    ).box[2] == 200
    # 130 vs 100 is 1.3, below the 1.35 requirement.
    with pytest.raises(MultipleFacesFound):
        _engine_with_rows([_row(130), _row(100)]).observe(_frame(), dominant_ratio=1.35)


def test_camera_check_reports_the_new_thresholds(logged_in):
    """The tuning page must show the hands-free settings it helps you set."""
    body = logged_in.get("/admin/camera-check").data.decode("utf-8")
    assert "AUTO_PRESENCE_THRESHOLD" in body
    assert "FACE_DOMINANT_RATIO" in body
    assert "presence-score" in body
    assert "PresenceDetector" in body
