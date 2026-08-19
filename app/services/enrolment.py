"""Enrolling an employee's face.

Enrolment quality decides recognition quality, so this module is stricter than
the kiosk: every sample must pass the quality gates, all samples must agree that
they show the same person, and no sample may look like an employee who is
already enrolled. That last check is what stops the same person being enrolled
twice under two payroll references - which would make every later scan ambiguous
and, worse, could let one person clock in as another.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import Flask, current_app

from ..extensions import db
from ..face.engine import (
    FaceError,
    FaceObservation,
    ModelsMissing,
    cosine_similarity,
    decode_image,
)
from ..models import Employee, FaceTemplate
from .recognition import get_engine, get_index, invalidate_index

# Two samples of one person should look far more alike than the acceptance
# threshold; if they do not, the operator has captured two different people or
# wildly inconsistent lighting.
SAME_PERSON_MIN = 0.55

# A new sample scoring above this against a *different* employee is refused.
CROSS_MATCH_MAX = 0.45


@dataclass
class EnrolmentOutcome:
    ok: bool
    code: str
    message: str
    added: int = 0
    rejected: list[str] = field(default_factory=list)
    clash_employee_id: int | None = None
    clash_score: float = 0.0


def _observe_all(frames: list[str | bytes], engine) -> tuple[list[FaceObservation], list[str]]:
    observations: list[FaceObservation] = []
    problems: list[str] = []
    for position, frame in enumerate(frames, start=1):
        try:
            observations.append(engine.observe(decode_image(frame)))
        except FaceError as exc:
            problems.append(f"Sample {position}: {exc.message}")
    return observations, problems


def enrol_employee(
    employee: Employee,
    frames: list[str | bytes],
    *,
    admin_id: int | None = None,
    replace_existing: bool = False,
    app: Flask | None = None,
) -> EnrolmentOutcome:
    """Add face templates for *employee* from captured *frames*."""
    app = app or current_app._get_current_object()  # type: ignore[attr-defined]
    config = app.config

    try:
        engine = get_engine(app)
    except ModelsMissing as exc:
        app.logger.error("Face models unavailable: %s", exc)
        return EnrolmentOutcome(
            False,
            "models_missing",
            "Face models are not installed on this server. "
            "Run: python scripts/fetch_models.py",
        )

    min_samples = config["ENROL_MIN_SAMPLES"]
    max_samples = config["ENROL_MAX_SAMPLES"]

    observations, problems = _observe_all(frames, engine)

    if len(observations) < min_samples:
        return EnrolmentOutcome(
            False,
            "too_few_samples",
            f"Need at least {min_samples} good samples, got {len(observations)}.",
            rejected=problems,
        )

    # Keep the sharpest samples if the operator captured more than we store.
    observations.sort(key=lambda obs: obs.sharpness, reverse=True)
    observations = observations[:max_samples]

    # All samples must plausibly be the same person.
    reference = observations[0].embedding
    for position, obs in enumerate(observations[1:], start=2):
        similarity = cosine_similarity(reference, obs.embedding)
        if similarity < SAME_PERSON_MIN:
            return EnrolmentOutcome(
                False,
                "samples_disagree",
                (
                    "The samples do not look like the same person "
                    f"(sample {position} scored {similarity:.2f}). "
                    "Please capture again with steady lighting."
                ),
                rejected=problems,
            )

    # Refuse to enrol a face that already belongs to somebody else.
    index = get_index(app)
    for obs in observations:
        for other_id, score in index.scores_for(obs.embedding).items():
            if other_id != employee.id and score > CROSS_MATCH_MAX:
                other = db.session.get(Employee, other_id)
                name = other.full_name if other else f"employee {other_id}"
                return EnrolmentOutcome(
                    False,
                    "already_enrolled",
                    (
                        f"This face already matches {name} (score {score:.2f}). "
                        "Remove that enrolment first if this is the same person."
                    ),
                    rejected=problems,
                    clash_employee_id=other_id,
                    clash_score=score,
                )

    if replace_existing:
        for template in list(employee.templates):
            db.session.delete(template)
        db.session.flush()

    for obs in observations:
        db.session.add(
            FaceTemplate(
                employee_id=employee.id,
                embedding=FaceTemplate.pack(obs.embedding),
                dimensions=int(obs.embedding.shape[0]),
                sharpness=obs.sharpness,
                face_pixels=obs.width,
                created_by_id=admin_id,
            )
        )

    db.session.commit()
    invalidate_index(app)

    return EnrolmentOutcome(
        True,
        "enrolled",
        f"Enrolled {len(observations)} face samples for {employee.full_name}.",
        added=len(observations),
        rejected=problems,
    )


def remove_enrolment(employee: Employee, *, app: Flask | None = None) -> int:
    """Delete every face template for *employee*. Returns how many were removed."""
    app = app or current_app._get_current_object()  # type: ignore[attr-defined]
    removed = 0
    for template in list(employee.templates):
        db.session.delete(template)
        removed += 1
    db.session.commit()
    invalidate_index(app)
    return removed
