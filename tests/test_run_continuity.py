"""Durable Aegra API Run ID -> logical Run mapping (ADR-0003 correction, #14).

This is the minimal, unambiguous slice of #14: the mapping problem ADR-0003's
2026-09-11 correction and ARCHITECTURE.md both flag as deferred. It carries no
approval inbox, authorization boundary, or dedupe policy for concurrent
submissions.
"""

import pytest

from cord_runtime.run_continuity import (
    RunContinuityError,
    UnknownThread,
    continuity_path,
    load_run_continuity,
    logical_run,
    record_submission,
)


def test_first_submission_becomes_its_own_logical_run(tmp_path):
    run_id = record_submission(tmp_path, "aegra-local", "t-1", "r-1")
    assert run_id == "r-1"
    assert logical_run(tmp_path, "aegra-local", "t-1") == {"run_id": "r-1", "api_run_ids": ["r-1"]}
    assert continuity_path(tmp_path).is_file()


def test_resume_submission_is_grouped_under_the_original_logical_run(tmp_path):
    record_submission(tmp_path, "aegra-local", "t-1", "r-1")
    run_id = record_submission(tmp_path, "aegra-local", "t-1", "r-2")
    assert run_id == "r-1"
    assert logical_run(tmp_path, "aegra-local", "t-1") == {
        "run_id": "r-1", "api_run_ids": ["r-1", "r-2"],
    }


def test_repeated_submission_of_the_same_api_run_id_is_idempotent(tmp_path):
    record_submission(tmp_path, "aegra-local", "t-1", "r-1")
    record_submission(tmp_path, "aegra-local", "t-1", "r-1")
    assert logical_run(tmp_path, "aegra-local", "t-1")["api_run_ids"] == ["r-1"]


def test_different_deployments_and_threads_stay_independent(tmp_path):
    record_submission(tmp_path, "aegra-local", "t-1", "r-1")
    record_submission(tmp_path, "aegra-staging", "t-1", "r-9")
    record_submission(tmp_path, "aegra-local", "t-2", "r-5")
    assert logical_run(tmp_path, "aegra-local", "t-1")["run_id"] == "r-1"
    assert logical_run(tmp_path, "aegra-staging", "t-1")["run_id"] == "r-9"
    assert logical_run(tmp_path, "aegra-local", "t-2")["run_id"] == "r-5"


def test_unknown_thread_raises(tmp_path):
    with pytest.raises(UnknownThread, match="aegra-local/t-1"):
        logical_run(tmp_path, "aegra-local", "t-1")


def test_lookup_before_any_write_does_not_create_a_file(tmp_path):
    with pytest.raises(UnknownThread):
        logical_run(tmp_path, "aegra-local", "t-1")
    assert not continuity_path(tmp_path).exists()
    assert load_run_continuity(tmp_path) == {}


@pytest.mark.parametrize("deployment,thread_id,api_run_id", [
    ("", "t-1", "r-1"),
    ("aegra-local", "  ", "r-1"),
    ("aegra-local", "t-1", ""),
])
def test_blank_identifiers_are_rejected(tmp_path, deployment, thread_id, api_run_id):
    with pytest.raises(RunContinuityError):
        record_submission(tmp_path, deployment, thread_id, api_run_id)


def test_malformed_file_raises_without_partial_reads(tmp_path):
    path = continuity_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RunContinuityError, match="not valid JSON"):
        load_run_continuity(tmp_path)


def test_non_object_file_is_rejected(tmp_path):
    path = continuity_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RunContinuityError, match="JSON object"):
        load_run_continuity(tmp_path)
