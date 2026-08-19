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
    """A confirmation is single use.

    This is what replaced the old minimum-interval rule. The interval blocked
    replays but also blocked genuine clocking; consuming the token blocks only
    the replay.
    """
    token = _identify(client)["confirm_token"]
    first = _commit(client, token).get_json()
    second = _commit(client, token).get_json()

    assert first["recorded"] is True
    assert second["ok"] is False
    assert second["code"] == "already_used"
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
def test_walking_past_mid_shift_now_offers_a_clock_out(client, db, enrolled):
    """The kiosk no longer refuses on the grounds of having clocked recently.

    This is a deliberate trade-off, chosen so the kiosk behaves as a plain
    toggle. The consequence is that somebody who steps in front of the camera
    mid-shift IS offered a clock-out, and the cancellable countdown is the only
    thing standing between them and a wrong entry - see AUTO_CONFIRM_SECONDS.
    The browser also requires them to have left the view first.
    """
    attendance.record_clock(
        enrolled,
        direction="in",
        occurred_at=utcnow() - dt.timedelta(seconds=5),
        cooldown_seconds=0,
    )

    payload = _identify(client)
    assert payload["code"] == "pending"
    assert payload["direction"] == "out"
    assert "seconds_until_next" not in payload


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


def test_the_buttons_still_work_alongside_hands_free(client, db, enrolled, app):
    """Pressing Clock out records immediately, whatever the automatic path did."""
    app.config["CLOCK_COOLDOWN_SECONDS"] = 0
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


def test_an_automatic_entry_records_without_a_cooldown(db):
    """Passing cooldown_seconds=0 records regardless of how recent the last was."""
    employee = make_employee(db)
    attendance.record_clock(employee, direction="in", cooldown_seconds=0)

    result = attendance.record_clock(
        employee, direction="out", cooldown_seconds=0, automatic=True
    )
    assert result.recorded
    assert db.session.query(AttendanceEvent).count() == 2


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


# --------------------------------------------------------------------------
# Toggle behaviour: in -> out, out -> in
# --------------------------------------------------------------------------
def test_clocked_in_is_offered_a_clock_out(client, db, enrolled):
    attendance.record_clock(
        enrolled,
        direction="in",
        occurred_at=utcnow() - dt.timedelta(seconds=30),
        cooldown_seconds=0,
    )
    assert attendance.is_clocked_in(enrolled.id)

    payload = _identify(client)
    assert payload["direction"] == "out"
    _commit(client, payload["confirm_token"])
    assert not attendance.is_clocked_in(enrolled.id)


def test_toggle_runs_both_ways(client, db, enrolled):
    """in -> out -> in, each sighting flipping the state."""
    seen = []
    for _ in range(3):
        payload = _identify(client)
        _commit(client, payload["confirm_token"])
        seen.append(payload["direction"])

    assert seen == ["in", "out", "in"]
    assert attendance.is_clocked_in(enrolled.id)


def test_the_server_no_longer_throttles_automatic_entries(client, db, enrolled):
    """Two sightings in a row both record; the throttle is the browser's.

    The server deliberately has no opinion about how recently somebody clocked.
    Preventing one approach from clocking twice is the browser's job - it waits
    for the person to leave the camera's view - and is covered by
    tests/js/kiosk_harness.js, which drives the real kiosk JavaScript.
    """
    first = _commit(client, _identify(client)["confirm_token"]).get_json()
    second = _commit(client, _identify(client)["confirm_token"]).get_json()

    assert first["recorded"] is True and first["direction"] == "in"
    assert second["recorded"] is True and second["direction"] == "out"
    assert db.session.query(AttendanceEvent).count() == 2


def test_range_settings_are_self_consistent(app):
    """Invariants, not exact numbers - the numbers are tunable per site.

    The browser upload width is the binding constraint on range: the server
    cannot recover detail the browser threw away, so uploading narrower frames
    than the detector will use silently caps how far away a face can be.
    """
    capture_width = app.config["CAPTURE_MAX_WIDTH"]
    detect_side = app.config["FACE_DETECT_MAX_SIDE"]

    assert capture_width >= detect_side, (
        f"CAPTURE_MAX_WIDTH ({capture_width}) is below FACE_DETECT_MAX_SIDE "
        f"({detect_side}): the extra server-side pixels can never exist"
    )
    # Measured floor: YuNet detects to ~30px, recognition holds to ~47px.
    assert app.config["FACE_MIN_PIXELS"] >= 40, "below the measured accuracy floor"


def test_speed_settings_are_sane(app):
    assert app.config["AUTO_SCAN_FRAMES"] >= 2, (
        "the liveness check compares frames, so it needs at least two"
    )
    assert app.config["AUTO_FRAME_GAP_MS"] >= 150, (
        "too short a gap leaves no real movement for the liveness check to see"
    )
    assert app.config["AUTO_CONFIRM_SECONDS"] >= 1, (
        "a countdown of zero removes the chance to cancel"
    )


def test_shipped_env_example_matches_the_code_defaults():
    """Guards against .env.example drifting from the defaults in config.py.

    Anybody installing this copies .env.example, so a stale value there silently
    becomes the deployed behaviour - which is exactly how tuned defaults got
    overridden during development.

    The comparison is against the defaults written in config.py's source, not
    against ``Config`` attributes: those resolve from whatever .env happens to be
    on this machine, so comparing them only ever proved that .env and
    .env.example agreed with each other.
    """
    import re

    from app.config import BASE_DIR

    source = (BASE_DIR / "app" / "config.py").read_text(encoding="utf-8")
    defaults = {
        name: value.strip()
        for name, value in re.findall(
            r'_(?:int|float|bool)\(\s*"([A-Z_]+)"\s*,\s*([^)]+)\)', source
        )
    }
    example = dict(
        re.findall(
            r"^([A-Z_]+)=(.*)$",
            (BASE_DIR / ".env.example").read_text(encoding="utf-8"),
            re.M,
        )
    )

    checked = 0
    for name, coded in defaults.items():
        if name not in example or example[name] == "":
            continue  # deliberately blank in the example, e.g. MYSQL_SSL_MODE
        expected = coded.strip().strip('"').strip("'")
        if expected in {"True", "False"}:
            expected = expected.lower()
        assert example[name] == expected, (
            f"{name}: .env.example says {example[name]!r} but config.py defaults "
            f"to {expected!r}"
        )
        checked += 1

    assert checked > 15, f"only compared {checked} settings; the parse likely broke"


def test_kiosk_page_carries_the_speed_and_range_settings(client):
    body = client.get("/").data.decode("utf-8")
    for key in ("autoFrames", "frameGapMs", "captureMaxWidth"):
        assert key in body, f"{key} missing from the kiosk page"


def test_a_small_distant_face_is_accepted_at_the_new_floor():
    """A 60px face must pass the size gate that an 80px floor rejected."""
    from app.face.engine import EngineSettings, FaceEngine, FaceTooSmall

    settings = EngineSettings(
        detector_model="x", recogniser_model="y", min_face_pixels=55
    )
    engine = FaceEngine.__new__(FaceEngine)
    engine.settings = settings
    engine._lock = threading.Lock()
    engine._detect_rows = lambda image: np.vstack([_row(60)])
    engine._embed_row = lambda image, row: (
        unit_vector(1), 500.0, np.zeros((112, 112, 3), dtype=np.uint8)
    )

    observation = engine.observe(np.zeros((480, 640, 3), dtype=np.uint8))
    assert observation.box[2] == 60

    # And something genuinely too far away is still refused.
    engine._detect_rows = lambda image: np.vstack([_row(30)])
    with pytest.raises(FaceTooSmall):
        engine.observe(np.zeros((480, 640, 3), dtype=np.uint8))


# --------------------------------------------------------------------------
# Toggling is gated on absence, not on elapsed time
# --------------------------------------------------------------------------
def test_every_sighting_toggles(client, db, enrolled):
    """Clocked out -> clocked in -> clocked out, with nothing in between."""
    seen = []
    for _ in range(4):
        payload = _identify(client)
        assert payload["code"] == "pending", payload
        assert payload["confirm_token"]
        _commit(client, payload["confirm_token"])
        seen.append(payload["direction"])

    assert seen == ["in", "out", "in", "out"]
    assert db.session.query(AttendanceEvent).count() == 4
    assert not attendance.is_clocked_in(enrolled.id)


def test_no_reply_ever_says_already_clocked(client, db, enrolled):
    """The refusal that used to exist must not come back."""
    attendance.record_clock(enrolled, direction="in", cooldown_seconds=0)
    payload = _identify(client)
    assert payload["code"] != "already_clocked"
    assert "already clocked" not in (payload.get("message") or "").lower()


def test_departure_settings_reach_the_kiosk_page(client, app):
    """The browser enforces departure, so it has to be told about it."""
    body = client.get("/").data.decode("utf-8")
    assert "requireDeparture" in body
    assert "departureMs" in body
    assert "requireDeparture: false" in body


def test_departure_gating_is_off_by_default(app):
    """Any recognised face is clocked, the same face included.

    With gating on, somebody the camera can always see was clocked once and then
    watched a screen that looked stuck. Off is the shipped default; the cost is
    that a person who stays in view is clocked repeatedly, with the countdown as
    the only guard.
    """
    assert app.config["AUTO_REQUIRE_DEPARTURE"] is False
    # Still meaningful for sites that switch gating back on.
    assert app.config["AUTO_DEPARTURE_MS"] >= 300, (
        "too short and somebody shifting their weight counts as leaving"
    )


def test_the_countdown_is_the_remaining_guard(app):
    """With no departure gating, the countdown is all that prevents a mis-clock."""
    assert app.config["AUTO_CONFIRM_SECONDS"] >= 1, (
        "AUTO_CONFIRM_SECONDS=0 with AUTO_REQUIRE_DEPARTURE=false leaves nothing "
        "at all between a passing glance at the camera and a recorded entry"
    )


def test_there_is_always_a_way_to_rearm(app):
    """The kiosk must never be able to get permanently stuck.

    Reported symptom: clocking worked on the way in and was unpredictable
    afterwards. The cause was relying on one fragile signal - a global
    grey-difference threshold - to decide somebody had left. There are now three
    independent ways to re-arm, and at least one of the two reliable ones must be
    enabled, whatever the .env says.
    """
    ways = []
    if app.config["AUTO_LATCHED_POLL_MS"] > 0:
        ways.append("server reports no face")
    if app.config["AUTO_REARM_SECONDS"] > 0:
        ways.append("timeout")

    assert ways, (
        "Both AUTO_LATCHED_POLL_MS and AUTO_REARM_SECONDS are disabled, leaving "
        "only the browser's presence check to notice somebody leaving. If that "
        "check never reports an empty scene the kiosk stops clocking entirely."
    )


def test_presence_check_can_never_block_clocking_outright(app):
    """A person the presence check cannot see must still be clocked eventually."""
    assert app.config["AUTO_IDLE_POLL_MS"] > 0 or app.config["AUTO_PRESENCE_THRESHOLD"] <= 3.0, (
        "With the idle safety poll disabled, anybody the grey-difference check "
        "misses - dark clothing, awkward angle, threshold set too high - is never "
        "recognised at all, with nothing on screen to explain why."
    )


def test_rearm_setting_reaches_the_kiosk_page(client):
    body = client.get("/").data.decode("utf-8")
    assert "rearmSeconds" in body


def test_debug_overlay_is_off_unless_asked_for(client):
    """Diagnostics must not be on the shop-floor screen by default."""
    assert "debug: false" in client.get("/").data.decode("utf-8")
    assert "debug: true" in client.get("/?debug=1").data.decode("utf-8")
