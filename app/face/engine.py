"""Face detection and embedding, built on OpenCV's bundled DNN models.

Two ONNX models from the OpenCV Model Zoo do all the work:

* **YuNet**  - a small, fast face detector.
* **SFace**  - produces a 128-value embedding per face, already L2-normalised,
  so cosine similarity between two embeddings is just their dot product.

Both load through ``cv2.dnn``, which means no PyTorch, no onnxruntime and no
dlib build step - the whole stack is one ``opencv-python-headless`` wheel.

The OpenCV net objects are *not* thread-safe, and both the Flask development
server and Waitress serve requests on threads, so every call into OpenCV is
serialised behind a lock. Inference is a few milliseconds per frame, so this is
not a practical bottleneck for a single kiosk.
"""

from __future__ import annotations

import base64
import binascii
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

EMBEDDING_DIMENSIONS = 128


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class FaceError(Exception):
    """Base class for recoverable face-processing problems.

    ``code`` is a stable machine-readable string for the JSON API; the message
    is shown to whoever is standing at the kiosk, so it stays plain and helpful.
    """

    code = "face_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ImageDecodeError(FaceError):
    code = "bad_image"


class NoFaceFound(FaceError):
    code = "no_face"


class MultipleFacesFound(FaceError):
    code = "multiple_faces"


class FaceTooSmall(FaceError):
    code = "face_too_small"


class FaceTooBlurred(FaceError):
    code = "face_too_blurred"


class ModelsMissing(RuntimeError):
    """Raised at start-up when the ONNX model files are not on disk."""


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EngineSettings:
    """Everything the engine needs, kept free of Flask so it can be unit tested."""

    detector_model: Path
    recogniser_model: Path
    max_side: int = 640
    detect_confidence: float = 0.85
    min_face_pixels: int = 80
    min_sharpness: float = 45.0


@dataclass(frozen=True)
class FaceObservation:
    """A single detected, quality-checked, embedded face."""

    box: tuple[int, int, int, int]
    score: float
    sharpness: float
    embedding: np.ndarray
    frame_shape: tuple[int, int]
    # The 112x112 aligned crop the embedding was taken from. Because alignment
    # removes position and scale, comparing crops across frames isolates real
    # changes in the face itself - which is what the liveness check needs.
    aligned: np.ndarray | None = None

    @property
    def width(self) -> int:
        return self.box[2]

    def box_as_fractions(self) -> tuple[float, float, float, float]:
        """Box as fractions of the frame, for drawing an overlay in the browser."""
        height, width = self.frame_shape
        x, y, w, h = self.box
        return (x / width, y / height, w / width, h / height)


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------
def decode_image(payload: str | bytes) -> np.ndarray:
    """Decode a data URL, a base64 string, or raw bytes into a BGR image."""
    if isinstance(payload, str):
        raw = payload.strip()
        if raw.startswith("data:"):
            _, _, raw = raw.partition(",")
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ImageDecodeError("That image could not be read.") from exc
    else:
        data = payload

    if not data:
        raise ImageDecodeError("No image data was supplied.")

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ImageDecodeError("That image could not be decoded.")
    return image


def fit_to_max_side(image: np.ndarray, max_side: int) -> np.ndarray:
    """Shrink *image* so its longest side is at most *max_side* pixels.

    YuNet is resolution-sensitive: handed a full-resolution phone photo it
    frequently finds nothing at all, so downscaling is a correctness measure,
    not just a speed one. Images already small enough are returned untouched.
    """
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image
    scale = max_side / longest
    return cv2.resize(
        image,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def sharpness_of(gray: np.ndarray) -> float:
    """Laplacian variance - a cheap, standard focus measure. Higher is sharper."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class FaceEngine:
    """Loads the models once and serialises access to them."""

    def __init__(self, settings: EngineSettings) -> None:
        self.settings = settings
        missing = [
            str(path)
            for path in (settings.detector_model, settings.recogniser_model)
            if not Path(path).is_file()
        ]
        if missing:
            raise ModelsMissing(
                "Face model(s) not found: "
                + ", ".join(missing)
                + ". Run: python scripts/fetch_models.py"
            )

        # Silence the "Targets are not supported by the new graph engine"
        # notice OpenCV 5 emits when a DNN target is selected.
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

        self._lock = threading.Lock()
        self._detector = cv2.FaceDetectorYN.create(
            str(settings.detector_model),
            "",
            (320, 320),
            settings.detect_confidence,
            0.3,
            5000,
        )
        self._recogniser = cv2.FaceRecognizerSF.create(str(settings.recogniser_model), "")

    # -- low level ---------------------------------------------------------
    def _detect_rows(self, image: np.ndarray) -> np.ndarray:
        """Return detector rows sorted by descending confidence.

        Each row is YuNet's raw 15-value output (box, five landmarks, score);
        ``alignCrop`` needs the whole row, so rows are passed around intact.
        """
        height, width = image.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(image)
        if faces is None or len(faces) == 0:
            return np.empty((0, 15), dtype=np.float32)
        return faces[np.argsort(-faces[:, -1])]

    def _embed_row(
        self, image: np.ndarray, row: np.ndarray
    ) -> tuple[np.ndarray, float, np.ndarray]:
        """Align the face described by *row*, then embed it.

        Returns the normalised embedding, the sharpness of the aligned crop and
        the crop itself. Measuring sharpness on the fixed 112x112 aligned crop
        (rather than the original frame) keeps the threshold meaningful whatever
        the camera resolution or how far away the person stands.
        """
        aligned = self._recogniser.alignCrop(image, row)
        feature = self._recogniser.feature(aligned)
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        sharp = sharpness_of(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY))
        return normalise(vector), sharp, aligned

    # -- public API --------------------------------------------------------
    def observe(
        self,
        image: np.ndarray,
        *,
        allow_multiple: bool = False,
        check_quality: bool = True,
        dominant_ratio: float | None = None,
    ) -> FaceObservation:
        """Detect, quality-check and embed the most prominent face in *image*.

        Raises a :class:`FaceError` subclass describing what went wrong, so the
        caller can tell the user "come closer" rather than a generic failure.

        When several faces are in view the behaviour depends on the caller:

        * default - refuse, because a press-to-scan kiosk should handle one
          person at a time;
        * *dominant_ratio* set - accept the nearest face provided it is at least
          that many times wider than the next one. On a shop floor somebody is
          usually walking past behind whoever is at the kiosk, and refusing
          every such frame would make hands-free clocking unusable. A face
          clearly closer to the camera is the person standing at it. If two
          faces are of similar size, nobody is clearly "at" the kiosk, so it
          still refuses rather than guessing;
        * *allow_multiple* - take the highest-confidence face and do not
          compare sizes (used by the camera diagnostics page).
        """
        prepared = fit_to_max_side(image, self.settings.max_side)

        with self._lock:
            rows = self._detect_rows(prepared)
            if len(rows) == 0:
                raise NoFaceFound("No face was found. Please face the camera.")

            if len(rows) > 1 and dominant_ratio is not None:
                # Widest box == nearest to the camera.
                by_width = rows[np.argsort(-rows[:, 2])]
                widest, runner_up = float(by_width[0][2]), float(by_width[1][2])
                if runner_up > 0 and widest < runner_up * dominant_ratio:
                    raise MultipleFacesFound(
                        "More than one person is in view. Please step up one at a time."
                    )
                rows = by_width
            elif len(rows) > 1 and not allow_multiple:
                raise MultipleFacesFound(
                    "More than one face is in view. Please step up one at a time."
                )

            row = rows[0]
            box = tuple(int(round(v)) for v in row[:4])
            if check_quality and box[2] < self.settings.min_face_pixels:
                raise FaceTooSmall("Please step a little closer to the camera.")

            embedding, sharp, aligned = self._embed_row(prepared, row)

        if check_quality and sharp < self.settings.min_sharpness:
            raise FaceTooBlurred("The image is too blurred. Please hold still.")

        return FaceObservation(
            box=box,  # type: ignore[arg-type]
            score=float(row[-1]),
            sharpness=sharp,
            embedding=embedding,
            frame_shape=prepared.shape[:2],
            aligned=aligned,
        )

    def observe_bytes(self, payload: str | bytes, **kwargs) -> FaceObservation:
        """Convenience wrapper: decode *payload* then :meth:`observe` it."""
        return self.observe(decode_image(payload), **kwargs)


# --------------------------------------------------------------------------
# Embedding maths
# --------------------------------------------------------------------------
def normalise(vector: np.ndarray) -> np.ndarray:
    """Return *vector* scaled to unit length (a no-op for SFace output).

    SFace already emits unit vectors, but normalising defensively means a
    stored embedding is always directly comparable by dot product, even if the
    model is swapped for one that does not normalise.
    """
    vec = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two embeddings, in the range -1.0 to 1.0."""
    return float(np.dot(normalise(a), normalise(b)))


def average_embedding(vectors: list[np.ndarray]) -> np.ndarray:
    """Mean of several embeddings of the same face, renormalised.

    Averaging a few frames suppresses per-frame noise and gives a slightly more
    reliable match than any single frame.
    """
    if not vectors:
        raise ValueError("At least one embedding is required.")
    stacked = np.vstack([normalise(v) for v in vectors])
    return normalise(stacked.mean(axis=0))
