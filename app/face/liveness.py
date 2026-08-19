"""A basic presentation-attack deterrent.

READ THIS BEFORE RELYING ON IT
------------------------------
This is *not* certified anti-spoofing. It compares the aligned face crops from
the frames of one scan and insists that something about the face changed between
them. A live face is never perfectly still - eyelids, mouth, tiny head rotations
- whereas a photo held up to the camera, or a frozen video stream, produces
near-identical aligned crops.

It will stop: a still photo on paper or a phone screen, and a stalled camera
feed sending the same frame repeatedly.

It will **not** stop: a video of the employee played back on a screen, a
convincing mask, or a determined attacker generally.

For a workshop clock-in kiosk in sight of a supervisor that trade-off is usually
the right one. If your risk assessment says otherwise, the honest options are a
supervised kiosk, a second factor (payroll PIN alongside the face - see
``LIVENESS_REQUIRE_MOTION`` and the README), or a camera with genuine depth or
infra-red liveness hardware.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .engine import FaceObservation, cosine_similarity


@dataclass(frozen=True)
class LivenessResult:
    passed: bool
    motion: float
    consistency: float
    reason: str = "ok"


def frame_motion(observations: list[FaceObservation]) -> float:
    """Mean absolute difference, in grey levels, between consecutive crops.

    Because the crops are *aligned*, simply moving a photo about in front of the
    camera does not register as motion - only a change in the face itself does.
    """
    crops = [obs.aligned for obs in observations if obs.aligned is not None]
    if len(crops) < 2:
        return 0.0

    greys = [cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32) for crop in crops]
    diffs = [
        float(np.mean(np.abs(greys[i] - greys[i - 1]))) for i in range(1, len(greys))
    ]
    return float(np.mean(diffs)) if diffs else 0.0


def frame_consistency(observations: list[FaceObservation]) -> float:
    """Lowest cosine similarity between any frame and the first frame.

    Guards the opposite failure to :func:`frame_motion`: if the frames are of
    *different people* - someone stepping aside mid-scan - the embeddings will
    not agree, and averaging them would produce a meaningless template.
    """
    if len(observations) < 2:
        return 1.0
    first = observations[0].embedding
    return min(cosine_similarity(first, obs.embedding) for obs in observations[1:])


def assess(
    observations: list[FaceObservation],
    *,
    require_motion: bool = True,
    min_motion: float = 1.6,
    min_consistency: float = 0.5,
) -> LivenessResult:
    """Judge whether a set of scan frames looks like a live person."""
    motion = frame_motion(observations)
    consistency = frame_consistency(observations)

    if consistency < min_consistency:
        return LivenessResult(False, motion, consistency, reason="frames_disagree")

    if require_motion:
        if len(observations) < 2:
            return LivenessResult(False, motion, consistency, reason="too_few_frames")
        if motion < min_motion:
            return LivenessResult(False, motion, consistency, reason="no_motion")

    return LivenessResult(True, motion, consistency)
