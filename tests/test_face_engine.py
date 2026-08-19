"""Engine, liveness and enrolment tests.

The tests marked ``needs_models`` run the real ONNX models against generated
images, and are skipped when the models have not been fetched.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.face.engine import (
    EngineSettings,
    FaceEngine,
    ImageDecodeError,
    ModelsMissing,
    NoFaceFound,
    average_embedding,
    cosine_similarity,
    decode_image,
    fit_to_max_side,
    normalise,
)
from app.face.liveness import assess, frame_consistency, frame_motion
from app.config import TestConfig

from .conftest import needs_models, unit_vector


# --- embedding maths ----------------------------------------------------------
def test_normalise_gives_unit_length():
    vector = normalise(np.array([3.0, 4.0], dtype=np.float32))
    assert np.linalg.norm(vector) == pytest.approx(1.0)


def test_normalise_survives_a_zero_vector():
    """A zero vector must not produce NaNs that poison every later comparison."""
    vector = normalise(np.zeros(4, dtype=np.float32))
    assert not np.isnan(vector).any()


def test_cosine_similarity_bounds():
    a = unit_vector(1)
    assert cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-5)
    assert cosine_similarity(a, -a) == pytest.approx(-1.0, abs=1e-5)


def test_average_embedding_is_normalised():
    average = average_embedding([unit_vector(1), unit_vector(2)])
    assert np.linalg.norm(average) == pytest.approx(1.0, abs=1e-5)


def test_average_embedding_needs_input():
    with pytest.raises(ValueError):
        average_embedding([])


# --- image handling -----------------------------------------------------------
def _jpeg_bytes(image):
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    return buffer.tobytes()


def test_decode_accepts_a_data_url():
    import base64

    blank = np.full((40, 40, 3), 128, dtype=np.uint8)
    data_url = "data:image/jpeg;base64," + base64.b64encode(_jpeg_bytes(blank)).decode()
    assert decode_image(data_url).shape == (40, 40, 3)


def test_decode_rejects_rubbish():
    with pytest.raises(ImageDecodeError):
        decode_image("not an image at all")
    with pytest.raises(ImageDecodeError):
        decode_image(b"")


def test_fit_to_max_side_shrinks_but_never_enlarges():
    big = np.zeros((2000, 1000, 3), dtype=np.uint8)
    assert fit_to_max_side(big, 640).shape[:2] == (640, 320)

    small = np.zeros((100, 80, 3), dtype=np.uint8)
    assert fit_to_max_side(small, 640).shape[:2] == (100, 80)


def test_missing_models_are_reported_clearly(tmp_path):
    with pytest.raises(ModelsMissing, match="fetch_models"):
        FaceEngine(
            EngineSettings(
                detector_model=tmp_path / "absent-detector.onnx",
                recogniser_model=tmp_path / "absent-recogniser.onnx",
            )
        )


# --- liveness -----------------------------------------------------------------
class _FakeObservation:
    """Enough of a FaceObservation for the liveness functions."""

    def __init__(self, embedding, aligned):
        self.embedding = embedding
        self.aligned = aligned


def _crop(value, noise=0, seed=0):
    base = np.full((112, 112, 3), value, dtype=np.uint8)
    if noise:
        rng = np.random.RandomState(seed)
        jitter = rng.randint(-noise, noise + 1, base.shape)
        base = np.clip(base.astype(int) + jitter, 0, 255).astype(np.uint8)
    return base


def test_identical_frames_read_as_a_photo():
    face = unit_vector(1)
    crop = _crop(120)
    observations = [_FakeObservation(face, crop), _FakeObservation(face, crop.copy())]

    assert frame_motion(observations) == pytest.approx(0.0)
    result = assess(observations, require_motion=True, min_motion=1.6)
    assert not result.passed
    assert result.reason == "no_motion"


def test_moving_frames_pass():
    face = unit_vector(1)
    observations = [
        _FakeObservation(face, _crop(120, noise=12, seed=1)),
        _FakeObservation(face, _crop(120, noise=12, seed=2)),
    ]
    assert frame_motion(observations) > 1.6
    assert assess(observations, require_motion=True, min_motion=1.6).passed


def test_two_different_people_in_one_scan_is_refused():
    observations = [
        _FakeObservation(unit_vector(1), _crop(120, noise=12, seed=1)),
        _FakeObservation(unit_vector(2), _crop(140, noise=12, seed=2)),
    ]
    assert frame_consistency(observations) < 0.5
    result = assess(observations)
    assert not result.passed
    assert result.reason == "frames_disagree"


def test_motion_check_can_be_switched_off():
    face = unit_vector(1)
    crop = _crop(120)
    observations = [_FakeObservation(face, crop), _FakeObservation(face, crop.copy())]
    assert assess(observations, require_motion=False).passed


def test_single_frame_cannot_satisfy_a_motion_check():
    observations = [_FakeObservation(unit_vector(1), _crop(120))]
    assert frame_consistency(observations) == 1.0
    assert not assess(observations, require_motion=True).passed


# --- the real models ----------------------------------------------------------
@pytest.fixture
def engine():
    return FaceEngine(
        EngineSettings(
            detector_model=TestConfig.FACE_DETECTOR_MODEL,
            recogniser_model=TestConfig.FACE_RECOGNISER_MODEL,
            min_sharpness=0.0,
            min_face_pixels=0,
        )
    )


@needs_models
def test_engine_finds_no_face_in_a_blank_image(engine):
    blank = np.full((480, 640, 3), 200, dtype=np.uint8)
    with pytest.raises(NoFaceFound):
        engine.observe(blank)


@needs_models
def test_engine_finds_no_face_in_noise(engine):
    noise = np.random.RandomState(0).randint(0, 256, (480, 640, 3)).astype(np.uint8)
    with pytest.raises(NoFaceFound):
        engine.observe(noise)


# Real photographs cannot be committed to the repository (they are personal data,
# and licensing them is a nuisance), so the accuracy check reads any images an
# operator drops into tests/fixtures/faces/ and skips when the folder is empty.
# See the README section "Checking accuracy with your own photos".
FIXTURE_FACES = Path(__file__).resolve().parent / "fixtures" / "faces"


def _fixture_images() -> list[Path]:
    if not FIXTURE_FACES.is_dir():
        return []
    return sorted(
        path
        for path in FIXTURE_FACES.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
    )


@needs_models
def test_engine_produces_a_128_value_unit_embedding(engine):
    images = _fixture_images()
    if not images:
        pytest.skip(f"No photos in {FIXTURE_FACES}; see the README to enable this check.")

    observation = engine.observe(cv2.imread(str(images[0])))
    assert observation.embedding.shape == (128,)
    assert np.linalg.norm(observation.embedding) == pytest.approx(1.0, abs=1e-4)
    assert observation.aligned is not None
    assert observation.aligned.shape == (112, 112, 3)
    assert observation.width >= 40


@needs_models
def test_same_person_scores_far_above_different_people(engine):
    """Naming convention: files starting "alice_" are one person, "bob_" another.

    Any prefix works - photos sharing a prefix before the first underscore are
    treated as the same person.
    """
    images = _fixture_images()
    groups: dict[str, list[np.ndarray]] = {}
    for path in images:
        try:
            observation = engine.observe(cv2.imread(str(path)))
        except NoFaceFound:
            continue
        groups.setdefault(path.stem.split("_")[0].lower(), []).append(observation.embedding)

    same_person = [
        cosine_similarity(vectors[i], vectors[j])
        for vectors in groups.values()
        for i in range(len(vectors))
        for j in range(i + 1, len(vectors))
    ]
    names = list(groups)
    different_people = [
        cosine_similarity(a, b)
        for i, first in enumerate(names)
        for second in names[i + 1 :]
        for a in groups[first]
        for b in groups[second]
    ]

    if not same_person or not different_people:
        pytest.skip(
            "Needs at least two photos of one person and one of somebody else in "
            f"{FIXTURE_FACES} (see the README)."
        )

    threshold = TestConfig.FACE_MATCH_THRESHOLD
    assert min(same_person) > threshold, (
        f"Two photos of the same person scored {min(same_person):.3f}, below the "
        f"{threshold} threshold - they would not be recognised."
    )
    assert max(different_people) < threshold, (
        f"Two different people scored {max(different_people):.3f}, above the "
        f"{threshold} threshold - one could clock in as the other."
    )
