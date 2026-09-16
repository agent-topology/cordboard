"""Durable Signal-ID dedupe claims (#17).

Uses controlled ids and an injected clock -- no real waiting -- to verify the
retention window, restart durability (a fresh call against the same on-disk
store still sees the earlier claim), and pruning.
"""

import pytest

from cord_runtime.signal_dedupe import DEFAULT_RETENTION_SECONDS, SignalDedupeError, claim, is_duplicate

NOW = 1_800_000_000.0


def test_first_claim_succeeds(tmp_path):
    assert claim(tmp_path, "manual:1", now=NOW) is True


def test_repeated_claim_within_window_is_rejected(tmp_path):
    assert claim(tmp_path, "manual:1", now=NOW) is True
    assert claim(tmp_path, "manual:1", now=NOW + 60) is False
    assert claim(tmp_path, "manual:1", now=NOW + DEFAULT_RETENTION_SECONDS - 1) is False


def test_claim_outside_retention_window_is_allowed_again(tmp_path):
    assert claim(tmp_path, "manual:1", now=NOW) is True
    assert claim(tmp_path, "manual:1", now=NOW + DEFAULT_RETENTION_SECONDS + 1) is True


def test_claim_survives_a_fresh_call_against_the_same_store(tmp_path):
    """A process restart re-reads the same on-disk store; the claim persists."""
    assert claim(tmp_path, "manual:1", now=NOW) is True
    # Simulate a restart: a brand-new call, no shared in-memory state.
    assert claim(tmp_path, "manual:1", now=NOW + 1) is False


def test_different_signal_ids_do_not_collide(tmp_path):
    assert claim(tmp_path, "manual:1", now=NOW) is True
    assert claim(tmp_path, "manual:2", now=NOW) is True


def test_is_duplicate_does_not_itself_claim(tmp_path):
    assert is_duplicate(tmp_path, "manual:1", now=NOW) is False
    assert is_duplicate(tmp_path, "manual:1", now=NOW) is False
    assert claim(tmp_path, "manual:1", now=NOW) is True
    assert is_duplicate(tmp_path, "manual:1", now=NOW) is True


def test_empty_signal_id_is_rejected(tmp_path):
    with pytest.raises(SignalDedupeError):
        claim(tmp_path, "", now=NOW)


def test_expired_claims_are_pruned_from_storage(tmp_path):
    claim(tmp_path, "manual:1", now=NOW)
    claim(tmp_path, "manual:2", now=NOW + DEFAULT_RETENTION_SECONDS + 1)
    from cord_runtime.signal_dedupe import load_signal_dedupe
    data = load_signal_dedupe(tmp_path)
    assert "manual:1" not in data
    assert "manual:2" in data
