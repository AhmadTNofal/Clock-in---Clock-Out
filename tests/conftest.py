"""Test fixtures.

The suite runs against SQLite in memory, so no MySQL server is needed. The face
models are only needed by the tests marked ``needs_models``; everything else uses
synthetic embeddings, which keeps the bulk of the suite fast and deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import AdminUser, Employee, FaceTemplate  # noqa: E402
from app.security import reset_rate_limits  # noqa: E402

MODELS_PRESENT = (
    TestConfig.FACE_DETECTOR_MODEL.is_file() and TestConfig.FACE_RECOGNISER_MODEL.is_file()
)

needs_models = pytest.mark.skipif(
    not MODELS_PRESENT, reason="ONNX models absent; run scripts/fetch_models.py"
)


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()
    reset_rate_limits()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(db):
    user = AdminUser(username="office", full_name="Office")
    user.set_password("correct-horse-battery")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def logged_in(client, admin):
    response = client.post(
        "/login",
        data={"username": "office", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 302, "admin login fixture failed"
    return client


def make_employee(db, ref="E001", first="Alice", last="Turner", **kwargs) -> Employee:
    employee = Employee(payroll_ref=ref, first_name=first, last_name=last, **kwargs)
    db.session.add(employee)
    db.session.commit()
    return employee


def unit_vector(seed: int, dimensions: int = 128) -> np.ndarray:
    """A deterministic pseudo-random unit vector, standing in for a real face."""
    rng = np.random.RandomState(seed)
    vector = rng.randn(dimensions).astype(np.float32)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def nudge(vector: np.ndarray, amount: float, seed: int = 99) -> np.ndarray:
    """Return a vector close to *vector* - a second photo of the same person."""
    rng = np.random.RandomState(seed)
    noise = rng.randn(vector.shape[0]).astype(np.float32)
    noise -= noise.dot(vector) * vector  # keep the noise orthogonal
    noise /= np.linalg.norm(noise)
    mixed = vector + amount * noise
    return (mixed / np.linalg.norm(mixed)).astype(np.float32)


def add_template(db, employee: Employee, vector: np.ndarray) -> FaceTemplate:
    template = FaceTemplate(
        employee_id=employee.id,
        embedding=FaceTemplate.pack(vector),
        dimensions=int(vector.shape[0]),
    )
    db.session.add(template)
    db.session.commit()
    return template


@pytest.fixture
def employee_factory(db):
    return lambda **kwargs: make_employee(db, **kwargs)
