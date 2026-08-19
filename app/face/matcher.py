"""Matching a probe embedding against the enrolled faces.

Kept free of Flask and of the database so the matching rules can be unit tested
directly. Because SFace embeddings are unit vectors, comparing one probe against
every enrolled template is a single matrix-vector product - fast enough that a
linear scan is the right answer well past any headcount a small manufacturer
will reach (thousands of templates is still sub-millisecond).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from .engine import normalise


@dataclass(frozen=True)
class MatchResult:
    """Outcome of comparing one probe embedding against the enrolled set."""

    employee_id: int | None
    score: float
    runner_up_id: int | None = None
    runner_up_score: float = 0.0
    reason: str = "matched"

    @property
    def accepted(self) -> bool:
        return self.employee_id is not None

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score


class FaceIndex:
    """An in-memory index of every enrolled template.

    One employee normally has several templates (different lighting, with and
    without safety glasses, and so on). A probe is scored against *all* of them
    and each employee keeps their single best score, so extra templates can only
    help recognition, never dilute it.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._employee_ids = np.zeros((0,), dtype=np.int64)

    # -- construction ------------------------------------------------------
    def load(self, templates: list[tuple[int, np.ndarray]]) -> None:
        """Replace the index contents with *templates* as (employee_id, vector)."""
        with self._lock:
            if not templates:
                self._matrix = np.zeros((0, 0), dtype=np.float32)
                self._employee_ids = np.zeros((0,), dtype=np.int64)
                return
            vectors = [normalise(vector) for _, vector in templates]
            width = len(vectors[0])
            if any(len(v) != width for v in vectors):
                raise ValueError("All embeddings must have the same length.")
            self._matrix = np.vstack(vectors).astype(np.float32)
            self._employee_ids = np.asarray([eid for eid, _ in templates], dtype=np.int64)

    @property
    def size(self) -> int:
        with self._lock:
            return int(self._matrix.shape[0])

    @property
    def employee_count(self) -> int:
        with self._lock:
            return int(np.unique(self._employee_ids).size) if self._employee_ids.size else 0

    # -- matching ----------------------------------------------------------
    def match(
        self, embedding: np.ndarray, *, threshold: float, margin: float = 0.0
    ) -> MatchResult:
        """Find the best-matching employee for *embedding*.

        A match is accepted only when it clears *threshold* **and** beats the
        best score from any *other* employee by at least *margin*. The margin
        guards against look-alikes: if two people score almost identically, the
        safe answer for a payroll record is to ask them to try again rather than
        to guess.
        """
        probe = normalise(embedding)

        with self._lock:
            if self._matrix.shape[0] == 0:
                return MatchResult(None, 0.0, reason="no_templates")
            if probe.shape[0] != self._matrix.shape[1]:
                raise ValueError(
                    f"Probe has {probe.shape[0]} dimensions, "
                    f"index has {self._matrix.shape[1]}."
                )
            scores = self._matrix @ probe
            employee_ids = self._employee_ids.copy()

        best_index = int(np.argmax(scores))
        best_id = int(employee_ids[best_index])
        best_score = float(scores[best_index])

        # Best score belonging to a *different* employee.
        others = scores[employee_ids != best_id]
        if others.size:
            runner_up_score = float(np.max(others))
            other_ids = employee_ids[employee_ids != best_id]
            runner_up_id = int(other_ids[int(np.argmax(others))])
        else:
            runner_up_score, runner_up_id = 0.0, None

        if best_score < threshold:
            return MatchResult(
                None, best_score, runner_up_id, runner_up_score, reason="below_threshold"
            )
        if best_score - runner_up_score < margin:
            return MatchResult(
                None, best_score, runner_up_id, runner_up_score, reason="ambiguous"
            )
        return MatchResult(best_id, best_score, runner_up_id, runner_up_score, reason="matched")

    def scores_for(self, embedding: np.ndarray) -> dict[int, float]:
        """Best score per employee - used by the admin enrolment quality check."""
        probe = normalise(embedding)
        with self._lock:
            if self._matrix.shape[0] == 0:
                return {}
            scores = self._matrix @ probe
            employee_ids = self._employee_ids.copy()
        best: dict[int, float] = {}
        for employee_id, score in zip(employee_ids.tolist(), scores.tolist()):
            if score > best.get(employee_id, -1.0):
                best[employee_id] = float(score)
        return best


def majority_match(results: list[MatchResult], min_agree: int) -> MatchResult | None:
    """Pick the employee that *min_agree* or more frames agree on.

    Requiring several frames of a scan to name the same person is what stops a
    single unlucky frame - a yawn, a hard shadow, someone walking past behind -
    from writing the wrong name into the attendance log.
    """
    accepted = [r for r in results if r.accepted]
    if not accepted:
        return None

    tally: dict[int, list[MatchResult]] = {}
    for result in accepted:
        tally.setdefault(int(result.employee_id or 0), []).append(result)

    # Most votes wins; ties break towards the higher confidence.
    employee_id, votes = max(
        tally.items(), key=lambda item: (len(item[1]), max(r.score for r in item[1]))
    )
    if len(votes) < min_agree:
        return None
    return max(votes, key=lambda r: r.score)
