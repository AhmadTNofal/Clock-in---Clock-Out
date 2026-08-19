"""Wires the face engine and the index to the database and to Flask.

The engine and the index are expensive to build (38 MB of model weights, plus a
scan of every enrolled template), so both are cached on the Flask app object and
shared by all request threads.

Index freshness is handled without any cache-invalidation infrastructure: the
index remembers how many templates it was built from and the highest template id
it saw, and re-checks that against two cheap aggregates at most once every
``INDEX_CHECK_INTERVAL`` seconds. Enrolling or deleting a face therefore takes
effect within seconds even if a second process made the change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from flask import Flask, current_app
from sqlalchemy import func, select

from ..extensions import db
from ..face.engine import (
    EngineSettings,
    FaceEngine,
    FaceError,
    FaceObservation,
    ModelsMissing,
    average_embedding,
    decode_image,
)
from ..face.liveness import LivenessResult, assess
from ..face.matcher import FaceIndex, MatchResult, majority_match
from ..models import Employee, FaceTemplate

INDEX_CHECK_INTERVAL = 10.0  # seconds

_ENGINE_KEY = "face_engine"
_INDEX_KEY = "face_index"


# --------------------------------------------------------------------------
# Cached singletons
# --------------------------------------------------------------------------
def engine_settings_from_config(config) -> EngineSettings:
    return EngineSettings(
        detector_model=config["FACE_DETECTOR_MODEL"],
        recogniser_model=config["FACE_RECOGNISER_MODEL"],
        max_side=config["FACE_DETECT_MAX_SIDE"],
        detect_confidence=config["FACE_DETECT_CONFIDENCE"],
        min_face_pixels=config["FACE_MIN_PIXELS"],
        min_sharpness=config["FACE_MIN_SHARPNESS"],
    )


def get_engine(app: Flask | None = None) -> FaceEngine:
    """Return the shared :class:`FaceEngine`, building it on first use."""
    app = app or current_app._get_current_object()  # type: ignore[attr-defined]
    engine = app.extensions.get(_ENGINE_KEY)
    if engine is None:
        engine = FaceEngine(engine_settings_from_config(app.config))
        app.extensions[_ENGINE_KEY] = engine
    return engine


@dataclass
class _CachedIndex:
    index: FaceIndex = field(default_factory=FaceIndex)
    template_count: int = -1
    max_template_id: int = -1
    checked_at: float = 0.0


def _template_fingerprint() -> tuple[int, int]:
    """(count, max id) for the template table - two cheap aggregates."""
    row = db.session.execute(
        select(func.count(FaceTemplate.id), func.coalesce(func.max(FaceTemplate.id), 0))
    ).one()
    return int(row[0]), int(row[1])


def _reload(cached: _CachedIndex) -> None:
    rows = db.session.execute(
        select(FaceTemplate.employee_id, FaceTemplate.embedding)
        .join(Employee, Employee.id == FaceTemplate.employee_id)
        .where(Employee.is_active.is_(True))
    ).all()

    import numpy as np

    templates = [
        (int(employee_id), np.frombuffer(blob, dtype=np.float32))
        for employee_id, blob in rows
    ]
    cached.index.load(templates)
    cached.template_count, cached.max_template_id = _template_fingerprint()
    cached.checked_at = time.monotonic()


def get_index(app: Flask | None = None) -> FaceIndex:
    """Return the shared face index, rebuilding it when templates have changed."""
    app = app or current_app._get_current_object()  # type: ignore[attr-defined]
    cached: _CachedIndex | None = app.extensions.get(_INDEX_KEY)
    if cached is None:
        cached = _CachedIndex()
        app.extensions[_INDEX_KEY] = cached
        _reload(cached)
        return cached.index

    if time.monotonic() - cached.checked_at >= INDEX_CHECK_INTERVAL:
        count, max_id = _template_fingerprint()
        if (count, max_id) != (cached.template_count, cached.max_template_id):
            _reload(cached)
        else:
            cached.checked_at = time.monotonic()
    return cached.index


def invalidate_index(app: Flask | None = None) -> None:
    """Force the index to rebuild on next use (called after enrolment changes)."""
    app = app or current_app._get_current_object()  # type: ignore[attr-defined]
    cached: _CachedIndex | None = app.extensions.get(_INDEX_KEY)
    if cached is not None:
        _reload(cached)


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ScanOutcome:
    """Result of processing the frames of one kiosk scan."""

    ok: bool
    code: str
    message: str
    employee_id: int | None = None
    score: float = 0.0
    observations: list[FaceObservation] = field(default_factory=list)
    liveness: LivenessResult | None = None
    match: MatchResult | None = None

    @property
    def embedding(self):
        """Averaged embedding across the accepted frames."""
        return average_embedding([obs.embedding for obs in self.observations])


def observe_frames(frames: list[str | bytes], engine: FaceEngine) -> tuple[list[FaceObservation], FaceError | None]:
    """Embed every frame, collecting the first error rather than aborting.

    A scan of three frames where one is blurred should still succeed, so errors
    are only fatal if *no* frame survives.
    """
    observations: list[FaceObservation] = []
    first_error: FaceError | None = None
    for frame in frames:
        try:
            observations.append(engine.observe(decode_image(frame)))
        except FaceError as exc:
            if first_error is None:
                first_error = exc
    return observations, first_error


def scan(frames: list[str | bytes], *, app: Flask | None = None) -> ScanOutcome:
    """Turn kiosk frames into an identified employee, or an explained refusal."""
    app = app or current_app._get_current_object()  # type: ignore[attr-defined]
    config = app.config

    if not frames:
        return ScanOutcome(False, "no_frames", "No image was received.")

    try:
        engine = get_engine(app)
    except ModelsMissing as exc:
        # Forgetting scripts/fetch_models.py is an easy install mistake; say so
        # in the log rather than returning an opaque 500 to the kiosk.
        app.logger.error("Face models unavailable: %s", exc)
        return ScanOutcome(
            False,
            "models_missing",
            "Face recognition is not set up on this server. Please see the office.",
        )

    observations, error = observe_frames(frames, engine)
    if not observations:
        assert error is not None
        return ScanOutcome(False, error.code, error.message)

    live = assess(
        observations,
        require_motion=config["LIVENESS_REQUIRE_MOTION"] and len(observations) > 1,
        min_motion=config["LIVENESS_MIN_MOTION"],
    )
    if not live.passed:
        message = (
            "Frames did not match each other. Please scan again on your own."
            if live.reason == "frames_disagree"
            else "Live camera check failed. Please look at the camera and scan again."
        )
        return ScanOutcome(
            False, f"liveness_{live.reason}", message, observations=observations, liveness=live
        )

    index = get_index(app)
    if index.size == 0:
        return ScanOutcome(
            False, "no_templates", "No faces are enrolled yet. Please see the office.",
            observations=observations, liveness=live,
        )

    threshold = config["FACE_MATCH_THRESHOLD"]
    margin = config["FACE_MATCH_MARGIN"]
    results = [
        index.match(obs.embedding, threshold=threshold, margin=margin) for obs in observations
    ]
    agreed = majority_match(results, min_agree=min(config["SCAN_MIN_AGREE"], len(observations)))

    if agreed is None:
        best = max(results, key=lambda r: r.score)
        if best.reason == "ambiguous":
            return ScanOutcome(
                False, "ambiguous", "Could not tell you apart from another record. Please see the office.",
                score=best.score, observations=observations, liveness=live, match=best,
            )
        return ScanOutcome(
            False, "not_recognised", "Face not recognised. Please try again or see the office.",
            score=best.score, observations=observations, liveness=live, match=best,
        )

    return ScanOutcome(
        True, "recognised", "Recognised.",
        employee_id=agreed.employee_id, score=agreed.score,
        observations=observations, liveness=live, match=agreed,
    )
