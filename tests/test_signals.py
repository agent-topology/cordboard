"""Signal construction (#16): deterministic identity per source, no payload
interpretation. Also covers the platform-emitted run.finished source (#18)."""

from datetime import datetime, timezone
import json

import pytest

from cord_runtime.signals import (
    InvalidSignal, file_signal, manual_signal, run_finished_signal, schedule_signal,
)


# --- manual_signal -----------------------------------------------------

def test_manual_signal_default_id_uses_timestamp_convention():
    now = datetime(2026, 9, 8, 14, 22, tzinfo=timezone.utc)
    signal = manual_signal({"a": 1}, now=now)
    assert signal == {"type": "manual", "id": f"manual:{now.isoformat()}", "payload": {"a": 1}}


def test_manual_signal_explicit_id_overrides_timestamp():
    signal = manual_signal({}, signal_id="manual:my-id")
    assert signal["id"] == "manual:my-id"


def test_manual_signal_rejects_non_dict_payload():
    with pytest.raises(InvalidSignal):
        manual_signal(["not", "a", "dict"])


def test_manual_signal_rejects_blank_explicit_id():
    with pytest.raises(InvalidSignal):
        manual_signal({}, signal_id="   ")


# --- file_signal ---------------------------------------------------------

def test_file_signal_reads_json_payload(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"path": "notes/api.md"}), encoding="utf-8")
    signal = file_signal(path)
    assert signal["type"] == "file"
    assert signal["payload"] == {"path": "notes/api.md"}
    assert signal["id"].startswith(f"file:{path}:")


def test_file_signal_same_content_same_id(tmp_path):
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    path_a.write_text(json.dumps({"k": "v"}), encoding="utf-8")
    path_b.write_text(json.dumps({"k": "v"}), encoding="utf-8")
    a = file_signal(path_a)
    b = file_signal(path_b)
    # Different paths always differ, but the content-hash suffix matches.
    assert a["id"].rsplit(":", 1)[1] == b["id"].rsplit(":", 1)[1]


def test_file_signal_different_content_different_id(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"k": "v1"}), encoding="utf-8")
    first = file_signal(path)
    path.write_text(json.dumps({"k": "v2"}), encoding="utf-8")
    second = file_signal(path)
    assert first["id"] != second["id"]


def test_file_signal_missing_file_raises():
    with pytest.raises(InvalidSignal, match="cannot read"):
        file_signal("/nonexistent/path/does-not-exist.json")


def test_file_signal_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidSignal, match="not valid JSON"):
        file_signal(path)


def test_file_signal_rejects_non_object_json(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(InvalidSignal, match="must contain a JSON object"):
        file_signal(path)


# --- schedule_signal -------------------------------------------------------

def test_schedule_signal_id_is_deterministic_for_the_same_tick():
    at = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    first = schedule_signal("daily-digest", at)
    second = schedule_signal("daily-digest", at)
    assert first == second
    assert first["id"] == f"schedule:daily-digest:{at.isoformat()}"
    assert first["payload"] == {"schedule": "daily-digest", "at": at.isoformat()}


def test_schedule_signal_merges_extra_payload():
    at = datetime(2026, 9, 16, tzinfo=timezone.utc)
    signal = schedule_signal("daily-digest", at, {"board": "acme"})
    assert signal["payload"]["board"] == "acme"


def test_schedule_signal_rejects_blank_name():
    with pytest.raises(InvalidSignal):
        schedule_signal("  ", datetime.now(timezone.utc))


# --- run_finished_signal (#18) ---------------------------------------------

def test_run_finished_signal_id_is_deterministic_on_run_id_alone():
    first = run_finished_signal(run_id="r-1", subject="urn:demo", connection="aegra-local",
                                 assistant="graph-a", status="success")
    second = run_finished_signal(run_id="r-1", subject="urn:demo", connection="aegra-local",
                                  assistant="graph-a", status="success")
    assert first == second
    assert first["id"] == "run.finished:r-1"
    assert first["type"] == "run.finished"


def test_run_finished_signal_carries_source_identity_and_depth():
    signal = run_finished_signal(run_id="r-9", subject="urn:demo", connection="aegra-local",
                                  assistant="graph-a", status="success", cascade_depth=2)
    assert signal["payload"] == {
        "run_id": "r-9", "subject": "urn:demo", "connection": "aegra-local",
        "assistant": "graph-a", "status": "success", "cascade_depth": 2,
    }


def test_run_finished_signal_defaults_to_depth_zero():
    signal = run_finished_signal(run_id="r-1", subject="urn:demo", connection="aegra-local",
                                  assistant="graph-a", status="success")
    assert signal["payload"]["cascade_depth"] == 0


def test_run_finished_signal_rejects_blank_run_id():
    with pytest.raises(InvalidSignal):
        run_finished_signal(run_id="  ", subject="urn:demo", connection="aegra-local",
                             assistant="graph-a", status="success")


def test_run_finished_signal_rejects_negative_depth():
    with pytest.raises(InvalidSignal):
        run_finished_signal(run_id="r-1", subject="urn:demo", connection="aegra-local",
                             assistant="graph-a", status="success", cascade_depth=-1)
