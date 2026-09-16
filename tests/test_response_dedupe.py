"""Cross-process claim/outcome dedupe for one interrupt's response (#44).

Mirrors `test_concurrency.py`'s style and its real-concurrent-arrival race
test: a bare read-then-write claim lets two real concurrent responses both
win, so the `_locked` guard is exercised the same deterministic way, with a
`threading.Barrier` forcing every worker to call `claim` at the same instant.
"""

import threading

import pytest

from cord_runtime.response_dedupe import (
    REJECTED,
    RESUMED,
    UNKNOWN,
    ResponseDedupeError,
    claim,
    get_claim,
    record_outcome,
)

NOW = 1_800_000_000.0


def test_first_claim_succeeds(tmp_path):
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW) is True


def test_second_claim_for_the_same_interrupt_is_a_duplicate(tmp_path):
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW) is True
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW + 1) is False


def test_a_different_interrupt_on_the_same_thread_claims_independently(tmp_path):
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW) is True
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-b", now=NOW) is True


def test_record_outcome_is_first_writer_wins(tmp_path):
    claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW)
    record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", RESUMED, now=NOW + 1)
    record = record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", UNKNOWN, now=NOW + 2)
    assert record["outcome"] == RESUMED
    assert record["resolved_at"] == NOW + 1


def test_unknown_outcome_is_durable_and_never_silently_overwritten(tmp_path):
    claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW)
    record = record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", UNKNOWN, now=NOW + 1)
    assert record["outcome"] == UNKNOWN
    again = record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", RESUMED, now=NOW + 2)
    assert again["outcome"] == UNKNOWN


def test_recording_an_outcome_without_a_claim_raises(tmp_path):
    with pytest.raises(ResponseDedupeError):
        record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", REJECTED, now=NOW)


def test_invalid_outcome_is_rejected(tmp_path):
    claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW)
    with pytest.raises(ResponseDedupeError):
        record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", "resolved", now=NOW)


def test_restart_shaped_reload_sees_the_same_claim(tmp_path):
    """Nothing in this module holds process memory between calls -- every
    call reloads from disk, so a later call is indistinguishable from one
    made after a process restart between the claim and the response (#44
    AC5). ``claimed_at`` stays the original value, not a fresh one."""
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW) is True
    assert claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW + 500) is False
    assert get_claim(tmp_path, "aegra-local", "thread-1", "interrupt-a")["claimed_at"] == NOW


def test_get_claim_returns_none_when_unclaimed(tmp_path):
    assert get_claim(tmp_path, "aegra-local", "thread-1", "interrupt-a") is None


def test_get_claim_reflects_the_recorded_outcome(tmp_path):
    claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW)
    record_outcome(tmp_path, "aegra-local", "thread-1", "interrupt-a", RESUMED, now=NOW + 1)
    assert get_claim(tmp_path, "aegra-local", "thread-1", "interrupt-a")["outcome"] == RESUMED


def test_empty_identifiers_are_rejected(tmp_path):
    with pytest.raises(ResponseDedupeError):
        claim(tmp_path, "", "thread-1", "interrupt-a", now=NOW)
    with pytest.raises(ResponseDedupeError):
        claim(tmp_path, "aegra-local", "", "interrupt-a", now=NOW)
    with pytest.raises(ResponseDedupeError):
        claim(tmp_path, "aegra-local", "thread-1", "", now=NOW)


# --- cross-process-shaped race: a bare read-then-write lets two concurrent
# responses to the same interrupt both proceed (#19's lesson, reapplied) ---

def test_real_concurrent_claims_for_the_same_interrupt_never_both_win(tmp_path):
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        claimed = claim(tmp_path, "aegra-local", "thread-1", "interrupt-a", now=NOW)
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
