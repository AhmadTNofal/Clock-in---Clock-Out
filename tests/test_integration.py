"""End-to-end test: real models, real HTTP round trip, real database writes.

Runs the whole path an installation actually takes - sign in, add an employee,
enrol their face through the admin endpoint, then clock them in and out through
the kiosk endpoint - with nothing stubbed except the camera itself.

Needs photos in tests/fixtures/faces/ (see the README), so it skips on a clean
checkout. Everything else in the suite runs without them.
"""

from __future__ import annotations

import base64

import cv2
import numpy as np
import pytest

from app.models import AttendanceEvent, Employee, FaceTemplate

from .conftest import needs_models
from .test_face_engine import _fixture_images

TOKEN = "test-kiosk-token"


def _data_url(image: np.ndarray) -> str:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


def _person_images() -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Return (photos of person A, photos of person B) grouped by filename prefix."""
    groups: dict[str, list[np.ndarray]] = {}
    for path in _fixture_images():
        image = cv2.imread(str(path))
        if image is not None:
            groups.setdefault(path.stem.split("_")[0].lower(), []).append(image)
    ordered = [images for _, images in sorted(groups.items())]
    first = ordered[0] if ordered else []
    second = ordered[1] if len(ordered) > 1 else []
    return first, second


def _vary(image: np.ndarray, index: int) -> np.ndarray:
    """Slight brightness / rotation variations, standing in for separate captures.

    Enrolment insists on several samples; a real operator gets them by capturing
    a few frames as the employee shifts slightly, which is what this imitates.
    """
    angle = (-3, 0, 3, -6, 6)[index % 5]
    gain = (1.0, 0.92, 1.08, 0.96, 1.04)[index % 5]
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated = cv2.warpAffine(image, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE)
    return cv2.convertScaleAbs(rotated, alpha=gain, beta=0)


@pytest.fixture
def fixture_people():
    first, second = _person_images()
    if len(first) < 2:
        pytest.skip(
            "Needs at least two photos of one person in tests/fixtures/faces/ "
            "(see the README)."
        )
    return first, second


@needs_models
def test_enrol_then_clock_in_and_out(logged_in, db, app, fixture_people):
    first, _ = fixture_people
    client = logged_in

    # --- add the employee -------------------------------------------------
    response = client.post(
        "/admin/employees/new",
        data={
            "payroll_ref": "E100",
            "first_name": "Sam",
            "last_name": "Fletcher",
            "department": "Assembly",
            "is_active": "y",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    employee = db.session.query(Employee).filter_by(payroll_ref="E100").one()

    # --- enrol their face -------------------------------------------------
    samples = [_data_url(_vary(first[0], i)) for i in range(3)]
    samples.append(_data_url(first[1]))
    enrol = client.post(
        f"/admin/employees/{employee.id}/enrol",
        json={"frames": samples, "replace": True},
    ).get_json()

    assert enrol["ok"] is True, enrol
    assert enrol["added"] >= app.config["ENROL_MIN_SAMPLES"]
    assert db.session.query(FaceTemplate).filter_by(employee_id=employee.id).count() >= 3

    # --- clock in through the kiosk --------------------------------------
    # Two genuinely different photos, so the liveness motion check is satisfied.
    scan_frames = [_data_url(first[0]), _data_url(first[1])]
    clock_in = client.post(
        "/api/kiosk/scan", json={"frames": scan_frames}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    assert clock_in["ok"] is True, clock_in
    assert clock_in["recorded"] is True
    assert clock_in["direction"] == "in"
    assert clock_in["employee"]["payroll_ref"] == "E100"
    assert clock_in["confidence"] > app.config["FACE_MATCH_THRESHOLD"]

    # --- clock out, with the cooldown disabled ---------------------------
    app.config["CLOCK_COOLDOWN_SECONDS"] = 0
    clock_out = client.post(
        "/api/kiosk/scan", json={"frames": scan_frames}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    assert clock_out["ok"] is True
    assert clock_out["direction"] == "out"

    events = db.session.query(AttendanceEvent).order_by(AttendanceEvent.id).all()
    assert [event.direction for event in events] == ["in", "out"]
    assert all(event.method == "face" for event in events)
    assert all(event.device_label for event in events)

    # --- and the timesheet shows the shift -------------------------------
    csv_response = client.get("/admin/timesheets.csv")
    assert csv_response.status_code == 200
    assert b"E100" in csv_response.data


@needs_models
def test_a_different_person_is_not_clocked_in(logged_in, db, fixture_people):
    """The headline safety property: person B must never clock in as person A."""
    first, second = fixture_people
    if not second:
        pytest.skip("Needs photos of a second person in tests/fixtures/faces/.")
    client = logged_in

    client.post(
        "/admin/employees/new",
        data={"payroll_ref": "E200", "first_name": "Ada", "last_name": "Reed", "is_active": "y"},
    )
    employee = db.session.query(Employee).filter_by(payroll_ref="E200").one()

    enrol = client.post(
        f"/admin/employees/{employee.id}/enrol",
        json={"frames": [_data_url(_vary(first[0], i)) for i in range(3)] + [_data_url(first[1])]},
    ).get_json()
    assert enrol["ok"] is True, enrol

    # Person B walks up to the kiosk.
    intruder = [_data_url(second[0]), _data_url(_vary(second[0], 2))]
    result = client.post(
        "/api/kiosk/scan", json={"frames": intruder}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    assert result["ok"] is False, f"A different person was recognised: {result}"
    assert result["code"] in {"not_recognised", "ambiguous"}
    assert db.session.query(AttendanceEvent).count() == 0


@needs_models
def test_the_same_face_cannot_be_enrolled_twice(logged_in, db, fixture_people):
    """Guards against one person holding two payroll references."""
    first, _ = fixture_people
    client = logged_in

    for ref, name in (("E301", "Jo"), ("E302", "Chris")):
        client.post(
            "/admin/employees/new",
            data={"payroll_ref": ref, "first_name": name, "last_name": "Doe", "is_active": "y"},
        )

    samples = [_data_url(_vary(first[0], i)) for i in range(3)] + [_data_url(first[1])]
    one = db.session.query(Employee).filter_by(payroll_ref="E301").one()
    two = db.session.query(Employee).filter_by(payroll_ref="E302").one()

    first_attempt = client.post(
        f"/admin/employees/{one.id}/enrol", json={"frames": samples}
    ).get_json()
    assert first_attempt["ok"] is True

    second_attempt = client.post(
        f"/admin/employees/{two.id}/enrol", json={"frames": samples}
    ).get_json()
    assert second_attempt["ok"] is False
    assert second_attempt["code"] == "already_enrolled"
    assert "Jo Doe" in second_attempt["message"]


@needs_models
def test_a_photo_held_up_to_the_camera_is_refused(logged_in, db, fixture_people, app):
    """The liveness deterrent: identical frames must not clock anybody in."""
    first, _ = fixture_people
    client = logged_in

    client.post(
        "/admin/employees/new",
        data={"payroll_ref": "E400", "first_name": "Ryan", "last_name": "Hale", "is_active": "y"},
    )
    employee = db.session.query(Employee).filter_by(payroll_ref="E400").one()
    enrol = client.post(
        f"/admin/employees/{employee.id}/enrol",
        json={"frames": [_data_url(_vary(first[0], i)) for i in range(3)] + [_data_url(first[1])]},
    ).get_json()
    assert enrol["ok"] is True

    # A still photo produces byte-identical frames.
    still = _data_url(first[0])
    result = client.post(
        "/api/kiosk/scan", json={"frames": [still, still]}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    assert result["ok"] is False
    assert result["code"] == "liveness_no_motion"
    assert db.session.query(AttendanceEvent).count() == 0


@needs_models
def test_hands_free_identify_then_commit_with_real_models(logged_in, db, app, fixture_people):
    """The full hands-free path, nothing stubbed except the camera."""
    first, _ = fixture_people
    client = logged_in

    client.post(
        "/admin/employees/new",
        data={"payroll_ref": "E500", "first_name": "Mia", "last_name": "Ford", "is_active": "y"},
    )
    employee = db.session.query(Employee).filter_by(payroll_ref="E500").one()
    enrol = client.post(
        f"/admin/employees/{employee.id}/enrol",
        json={"frames": [_data_url(_vary(first[0], i)) for i in range(3)] + [_data_url(first[1])]},
    ).get_json()
    assert enrol["ok"] is True, enrol

    frames = [_data_url(first[0]), _data_url(first[1])]

    # Identify: recognises, writes nothing.
    identified = client.post(
        "/api/kiosk/identify", json={"frames": frames}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()
    assert identified["ok"] is True, identified
    assert identified["code"] == "pending"
    assert identified["employee"]["payroll_ref"] == "E500"
    assert identified["direction"] == "in"
    assert db.session.query(AttendanceEvent).count() == 0

    # Commit: now it exists.
    committed = client.post(
        "/api/kiosk/commit",
        json={"confirm_token": identified["confirm_token"]},
        headers={"X-Kiosk-Token": TOKEN},
    ).get_json()
    assert committed["ok"] is True
    assert committed["recorded"] is True
    event = db.session.query(AttendanceEvent).one()
    assert event.direction == "in"
    assert event.method == "auto"

    # Standing there a moment longer must not clock them out again.
    again = client.post(
        "/api/kiosk/identify", json={"frames": frames}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()
    assert again["code"] == "already_clocked"
    assert db.session.query(AttendanceEvent).count() == 1


@needs_models
def test_hands_free_ignores_an_unknown_person(logged_in, db, fixture_people):
    """A visitor walking past must not produce a pending clock-in."""
    first, second = fixture_people
    if not second:
        pytest.skip("Needs photos of a second person in tests/fixtures/faces/.")
    client = logged_in

    client.post(
        "/admin/employees/new",
        data={"payroll_ref": "E600", "first_name": "Kai", "last_name": "Webb", "is_active": "y"},
    )
    employee = db.session.query(Employee).filter_by(payroll_ref="E600").one()
    client.post(
        f"/admin/employees/{employee.id}/enrol",
        json={"frames": [_data_url(_vary(first[0], i)) for i in range(3)] + [_data_url(first[1])]},
    )

    result = client.post(
        "/api/kiosk/identify",
        json={"frames": [_data_url(second[0]), _data_url(_vary(second[0], 2))]},
        headers={"X-Kiosk-Token": TOKEN},
    ).get_json()

    assert result["ok"] is False
    assert result["code"] in {"not_recognised", "ambiguous"}
    assert "confirm_token" not in result
    assert db.session.query(AttendanceEvent).count() == 0
