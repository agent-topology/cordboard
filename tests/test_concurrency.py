"""Declared (Assistant, Subject) concurrency claims, skip default (#17).

Controlled ids and an injected clock exercise concurrent arrival, release,
and stale-claim recovery after a crashed holder without any real waiting.
"""

import pytest

from cord_runtime.concurrency import ConcurrencyError, release, try_claim

NOW = 1_800_000_000.0


def test_first_claim_succeeds(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True


def test_concurrent_arrival_for_the_same_pair_is_skipped(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW + 1, stale_after=180) is False


def test_different_subject_on_the_same_assistant_does_not_conflict(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True
    assert try_claim(tmp_path, "triage-graph", "github:issue/2", now=NOW, stale_after=180) is True


def test_different_assistant_on_the_same_subject_does_not_conflict(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True
    assert try_claim(tmp_path, "other-graph", "github:issue/1", now=NOW, stale_after=180) is True


def test_release_lets_a_later_arrival_claim_the_pair(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True
    release(tmp_path, "triage-graph", "github:issue/1")
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW + 1, stale_after=180) is True


def test_release_of_an_unheld_pair_is_a_no_op(tmp_path):
    release(tmp_path, "triage-graph", "github:issue/1")  # does not raise


def test_a_stale_claim_from_a_crashed_holder_is_reclaimed(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True
    # No release() -- the holder "crashed" before its finally ran.
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW + 179, stale_after=180) is False
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW + 181, stale_after=180) is True


def test_empty_assistant_or_subject_is_rejected(tmp_path):
    with pytest.raises(ConcurrencyError):
        try_claim(tmp_path, "", "github:issue/1", now=NOW, stale_after=180)
    with pytest.raises(ConcurrencyError):
        try_claim(tmp_path, "triage-graph", "", now=NOW, stale_after=180)
