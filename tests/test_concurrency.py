"""Declared (Assistant, Subject) concurrency claims, skip default (#17).

Controlled ids and an injected clock exercise concurrent arrival, release,
and stale-claim recovery after a crashed holder without any real waiting.
"""

import threading

import pytest

from cord_runtime.concurrency import ConcurrencyError, release, renew, try_claim

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


# --- #50: a claim held for a Run that outlives one call renews instead of
# --- expiring under its own crash-recovery window --------------------------

def test_renew_bumps_claimed_at_so_a_long_held_claim_does_not_go_stale(tmp_path):
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True
    # Without a renewal, this claim would already be reclaimable here.
    renew(tmp_path, "triage-graph", "github:issue/1", now=NOW + 170)
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW + 179, stale_after=180) is False
    # And it is still live a further 170s after the renewal, well past the
    # original claim's own stale_after window measured from NOW.
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW + 340, stale_after=180) is False


def test_renew_of_an_unheld_pair_is_a_no_op(tmp_path):
    renew(tmp_path, "triage-graph", "github:issue/1", now=NOW)  # does not raise
    assert try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180) is True


def test_empty_assistant_or_subject_is_rejected(tmp_path):
    with pytest.raises(ConcurrencyError):
        try_claim(tmp_path, "", "github:issue/1", now=NOW, stale_after=180)
    with pytest.raises(ConcurrencyError):
        try_claim(tmp_path, "triage-graph", "", now=NOW, stale_after=180)


# --- #19: a bare read-then-write let two real concurrent arrivals both win --

def test_real_concurrent_arrivals_for_the_same_pair_never_both_claim(tmp_path):
    """`try_claim` is a check-and-set: `load_concurrency` followed later by
    `_write_atomic` is two separate file operations, so without a lock
    spanning both, two threads (or processes) can each read "unclaimed"
    before either writes and both return `True` for the same pair --
    silently violating the "skip" policy every other test in this file
    exercises only sequentially. A `threading.Barrier` forces every worker to
    call `try_claim` at the same instant so the race is deterministic rather
    than timing-dependent.
    """
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        claimed = try_claim(tmp_path, "triage-graph", "github:issue/1", now=NOW, stale_after=180)
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
