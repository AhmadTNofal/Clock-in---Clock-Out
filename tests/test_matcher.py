"""Matching rules: threshold, margin and frame voting."""

from __future__ import annotations

import numpy as np
import pytest

from app.face.matcher import FaceIndex, MatchResult, majority_match

from .conftest import nudge, unit_vector


def test_empty_index_never_matches():
    index = FaceIndex()
    result = index.match(unit_vector(1), threshold=0.4)
    assert not result.accepted
    assert result.reason == "no_templates"


def test_matches_the_right_employee():
    index = FaceIndex()
    alice, bob = unit_vector(1), unit_vector(2)
    index.load([(10, alice), (20, bob)])

    result = index.match(nudge(alice, 0.3), threshold=0.4, margin=0.05)
    assert result.employee_id == 10
    assert result.score > 0.9
    assert result.reason == "matched"


def test_stranger_is_rejected():
    index = FaceIndex()
    index.load([(10, unit_vector(1)), (20, unit_vector(2))])

    result = index.match(unit_vector(999), threshold=0.4)
    assert not result.accepted
    assert result.reason == "below_threshold"


def test_margin_rejects_look_alikes():
    """Two employees with near-identical templates must not be guessed between."""
    index = FaceIndex()
    base = unit_vector(7)
    index.load([(10, base), (20, nudge(base, 0.02))])

    result = index.match(base, threshold=0.4, margin=0.05)
    assert not result.accepted
    assert result.reason == "ambiguous"
    # Without a margin requirement the same probe does match.
    assert index.match(base, threshold=0.4, margin=0.0).employee_id == 10


def test_extra_templates_only_help():
    """A second template for one employee must not dilute their best score."""
    index = FaceIndex()
    alice = unit_vector(1)
    index.load([(10, alice), (10, unit_vector(50)), (20, unit_vector(2))])

    result = index.match(alice, threshold=0.4, margin=0.05)
    assert result.employee_id == 10
    assert result.score == pytest.approx(1.0, abs=1e-5)


def test_inactive_employee_absent_from_index_is_not_matched():
    index = FaceIndex()
    index.load([(20, unit_vector(2))])
    assert index.match(unit_vector(1), threshold=0.4).employee_id is None


def test_dimension_mismatch_is_explicit():
    index = FaceIndex()
    index.load([(10, unit_vector(1, 128))])
    with pytest.raises(ValueError, match="dimensions"):
        index.match(unit_vector(1, 64), threshold=0.4)


def test_mixed_dimensions_rejected_on_load():
    index = FaceIndex()
    with pytest.raises(ValueError, match="same length"):
        index.load([(10, unit_vector(1, 128)), (20, unit_vector(2, 64))])


def test_scores_for_returns_best_per_employee():
    index = FaceIndex()
    alice = unit_vector(1)
    index.load([(10, alice), (10, unit_vector(50)), (20, unit_vector(2))])

    scores = index.scores_for(alice)
    assert set(scores) == {10, 20}
    assert scores[10] == pytest.approx(1.0, abs=1e-5)
    assert scores[20] < 0.5


# --- frame voting -------------------------------------------------------------
def _result(employee_id, score):
    return MatchResult(employee_id, score, reason="matched" if employee_id else "below_threshold")


def test_majority_needs_enough_agreement():
    assert majority_match([_result(1, 0.8), _result(None, 0.1), _result(None, 0.1)], 2) is None


def test_majority_picks_agreed_employee():
    agreed = majority_match([_result(1, 0.7), _result(1, 0.9), _result(None, 0.2)], 2)
    assert agreed is not None
    assert agreed.employee_id == 1
    # The confidence reported is the best of the agreeing frames.
    assert agreed.score == pytest.approx(0.9)


def test_majority_rejects_a_split_vote():
    """Two frames naming two different people is not a decision."""
    assert majority_match([_result(1, 0.8), _result(2, 0.8)], 2) is None


def test_majority_ignores_unmatched_frames():
    assert majority_match([_result(None, 0.0)], 1) is None
