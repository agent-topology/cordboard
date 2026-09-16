"""Sanitized, evidence-based archive delivery health (#45).

`archive_query.read_spans` is the strict record-of-origin reader (ADR-0005):
any unparseable or structurally invalid line fails the whole read, because
`archive-escalations` and `cord view` require complete Run trees. That
contract is intentionally unforgiving of an actively-written file, so it is
unusable as a *health* signal for a file the Collector may still be
appending to.

This module adds a second, tolerant reading path plus the documented health
vocabulary a caller (CLI, viewer) classifies evidence against. It never
infers a failure from silence and never invents a count the archive itself
does not contain.

Vocabulary
----------
``PENDING``     -- No archive files exist yet at all for the given paths.
                    Absent evidence, distinct from a file that exists and is
                    merely empty or mid-write; requires a positive control
                    (#45 AC1) before it is read as anything stronger.
``EMPTY``       -- Archive files exist but hold no complete spans yet
                    (freshly created, or the only content is an in-progress
                    write). Still not evidence of failure by itself.
``INCOMPLETE``  -- At least one complete span was read, and the most recent
                    archive file's trailing line looks like an append still
                    in progress (the Collector's own ``group_by`` keeps at
                    most one open file per date partition). Every earlier,
                    complete record still reads normally; only the
                    unfinished line is set aside (#45 AC3).
``FAILED``      -- A complete record fails to parse or violates the archive
                    contract (`archive_query.ArchiveError`). A real
                    corruption/loss signal, never concealed as EMPTY.
``HEALTHY``     -- At least one span read successfully and no file's tail
                    looked incomplete.

`ArchiveHealth.detail` is always one of `archive_query`'s own fixed,
value-free diagnostic strings, or a fixed string from this module -- never
archive payload or a file's raw content (#45 AC5).
"""

from dataclasses import dataclass
from enum import Enum
import gzip
import json
from pathlib import Path
import tempfile

from cord_runtime.archive_query import ArchiveError, read_spans


class HealthStatus(Enum):
    PENDING = "pending"
    EMPTY = "empty"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    HEALTHY = "healthy"


@dataclass(frozen=True)
class ArchiveHealth:
    status: HealthStatus
    span_count: int
    incomplete_files: tuple[str, ...]
    detail: str | None = None


def _files(paths: list[Path]) -> list[Path]:
    """Resolve `paths` (directories or individual files) to archive files,
    tolerating a directory with no matches yet -- unlike
    `archive_query.scan`, absence here is PENDING evidence, not an error."""
    files = []
    for path in paths:
        entries = (sorted(path.glob("*.otlp.jsonl")) + sorted(path.glob("*.otlp.jsonl.gz"))
                   if path.is_dir() else [path])
        files.extend(entries)
    return files


def _visible_lines(path: Path) -> tuple[list[str], bool]:
    """This file's complete lines, with one trailing in-progress append set
    aside. Only a plain (open, not yet rotated) `.otlp.jsonl` file can still
    be mid-write; a `.gz` partition is already closed, so it is always read
    strictly. Returns (complete_lines, tail_was_incomplete)."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.readlines(), False
    with open(path, "rt", encoding="utf-8") as handle:
        lines = handle.readlines()
    if not lines:
        return lines, False
    try:
        json.loads(lines[-1])
        return lines, False
    except ValueError:
        return lines[:-1], True


def read_active_spans(paths: list[Path]) -> tuple[dict, tuple[str, ...]]:
    """Tolerant counterpart to `archive_query.read_spans` for live health
    reads: tolerate exactly one in-progress trailing write per file without
    accepting a genuinely corrupt *complete* record. Any decode/contract
    failure on a complete line still raises `ArchiveError`, unchanged --
    this never silently accepts corruption, it only stops treating an
    unfinished last line as corruption (#45 AC3).

    Returns (spans, incomplete_files) -- `incomplete_files` names each
    archive file (by its own name only, never a directory path) whose
    trailing line was set aside as still in flight.

    Each file is snapshotted to a private temporary copy before parsing, so
    a file that keeps growing between this read and `read_spans`'s own
    re-open never changes which bytes were actually evaluated.
    """
    files = _files(paths)
    incomplete_files = []
    with tempfile.TemporaryDirectory(prefix="cord-archive-health-") as tmp:
        materialized = []
        for index, path in enumerate(files):
            lines, tail_incomplete = _visible_lines(path)
            if tail_incomplete:
                incomplete_files.append(path.name)
            if lines:
                snapshot = Path(tmp) / f"{index:04d}.otlp.jsonl"
                snapshot.write_text("".join(lines), encoding="utf-8")
                materialized.append(snapshot)
        spans = read_spans(materialized) if materialized else {}
    return spans, tuple(incomplete_files)


def archive_health(paths: list[Path]) -> ArchiveHealth:
    """Classify the given archive paths against the vocabulary above.

    `paths` follows `archive_query.read_spans`'s own contract: each entry is
    either an archive directory (globbed for `*.otlp.jsonl[.gz]`) or one
    explicit file.
    """
    if not _files(paths):
        return ArchiveHealth(HealthStatus.PENDING, 0, (), "no archive files present yet")
    try:
        spans, incomplete_files = read_active_spans(paths)
    except ArchiveError as exc:
        return ArchiveHealth(HealthStatus.FAILED, 0, (), str(exc))
    if not spans:
        if incomplete_files:
            return ArchiveHealth(HealthStatus.INCOMPLETE, 0, incomplete_files,
                                  "archive file has only an in-progress write")
        return ArchiveHealth(HealthStatus.EMPTY, 0, (), "archive file has no spans yet")
    status = HealthStatus.INCOMPLETE if incomplete_files else HealthStatus.HEALTHY
    return ArchiveHealth(status, len(spans), incomplete_files, None)


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    health = archive_health(args.paths)
    print(json.dumps({
        "status": health.status.value,
        "span_count": health.span_count,
        "incomplete_files": list(health.incomplete_files),
        "detail": health.detail,
    }, ensure_ascii=False, indent=2), file=sys.stdout)
    return 0 if health.status in (HealthStatus.HEALTHY, HealthStatus.EMPTY, HealthStatus.PENDING) else 1


if __name__ == "__main__":
    raise SystemExit(main())
