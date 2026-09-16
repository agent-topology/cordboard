"""Count explicitly escalated Attempts in complete raw OTLP JSONL archives."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import sys

from cord_runtime.execution import AttemptOutcome, StepOutcome, SEMCONV_VERSION

WINDOW_NS = 56 * 86400 * 1_000_000_000
IDENTITY = ("cord.graph.id", "cord.run.id", "cord.subject.id", "cord.subject.type")


class ArchiveError(ValueError):
    """Fixed diagnostic with a record location, never archive payload values."""


def reference_ns(value: str) -> int:
    """Parse an explicit timezone-aware ISO time without floating point rounding."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000
    except ValueError:
        raise argparse.ArgumentTypeError("reference time requires ISO-8601 with a timezone") from None


def require(condition, reason):
    if not condition:
        raise ArchiveError(reason)


def attributes(span):
    result = {}
    for attr in span.get("attributes", []):
        key, value = attr["key"], attr["value"]
        require(key not in result and len(value) == 1, "duplicate or malformed attribute")
        kind, item = next(iter(value.items()))
        if kind == "intValue":
            require(type(item) is int or (isinstance(item, str) and item.isdecimal()), "invalid integer attribute")
            item = int(item)
        elif kind == "stringValue":
            require(isinstance(item, str), "invalid string attribute")
        elif kind == "boolValue":
            require(type(item) is bool, "invalid boolean attribute")
        result[key] = item
    return result


def identifier(value, length):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{%d}" % length, value)
            and int(value, 16) != 0, "invalid OTLP trace/span identifier")
    return value.lower()


def timestamp(value):
    require((type(value) is int or (isinstance(value, str) and value.isdecimal())),
            "invalid span timestamp")
    value = int(value)
    require(0 < value < 2**64, "invalid span timestamp")
    return value


def scan(paths):
    """Resolve `paths` (directories or individual files) to the ordered list
    of archive files `read_spans` would read. Shared with `archive_health`
    (#45) so tail tolerance sees the exact same file set/order."""
    files = []
    for path in paths:
        entries = (sorted(path.glob("*.otlp.jsonl")) + sorted(path.glob("*.otlp.jsonl.gz"))
                   if path.is_dir() else [path])
        require(entries, "archive directory has no input files")
        files.extend(entries)
    return files


def read_spans(paths):
    """Read all partitions before joining parents; exporter order is arbitrary."""
    indexed = {}
    files = scan(paths)
    for file_number, path in enumerate(files, 1):
        line_number = 0
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    record = json.loads(line)
                    require(isinstance(record["resourceSpans"], list), "invalid OTLP envelope")
                    for resource in record["resourceSpans"]:
                        for scope in resource["scopeSpans"]:
                            for span in scope["spans"]:
                                sid = identifier(span["spanId"], 16)
                                span["spanId"] = sid
                                span["traceId"] = identifier(span["traceId"], 32)
                                if span.get("parentSpanId"):
                                    span["parentSpanId"] = identifier(span["parentSpanId"], 16)
                                attrs = attributes(span)
                                start = timestamp(span["startTimeUnixNano"])
                                end = timestamp(span["endTimeUnixNano"])
                                require(end >= start, "span ends before it starts")
                                # Resource receipt-date changes do not change span identity.
                                normalized = {**span, "attributes": attrs,
                                              "startTimeUnixNano": start, "endTimeUnixNano": end}
                                if sid in indexed:
                                    require(indexed[sid][0] == normalized, "conflicting duplicate span_id")
                                else:
                                    indexed[sid] = (normalized, f"file {file_number}, line {line_number}")
            require(line_number, "empty archive file")
        except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, ArchiveError) else "invalid or unreadable OTLP JSONL"
            raise ArchiveError(f"file {file_number}, line {line_number}: {reason}") from None
    require(indexed, "archive contains no spans")
    return indexed


def query(paths: list[Path], *, reference: int, top: int = 10) -> list[dict]:
    require(type(top) is int and top > 0, "top must be a positive integer")
    return query_spans(read_spans(paths), reference=reference, top=top)


def role(span):
    """Classify one normalized span as a Run, Step, or Attempt, or None for an
    ordinary (non-Cordboard) span. Shared by every reader of the archive
    contract; do not reimplement this classification elsewhere (#13)."""
    attrs = span["attributes"]
    name = span.get("name", "")
    require(isinstance(name, str), "invalid span name")
    if name == "attempt" or "cord.step.attempt" in attrs:
        return "attempt"
    if name.startswith("step:") or "cord.node.name" in attrs:
        return "step"
    if name == "run" or "cord.semconv.version" in attrs:
        return "run"
    require("cord.outcome" not in attrs, "unclassified Cordboard outcome span")
    return None  # ordinary proxy/model spans are not Attempts


def parent(spans, span, expected):
    """`span`'s validated immediate parent, which must have role `expected`
    and share `IDENTITY` with it. Shared by every reader of the archive
    contract (#13)."""
    entry = spans.get(span.get("parentSpanId"))
    require(entry is not None, f"{expected} parent is missing; supply complete Run trees")
    result = entry[0]
    require(role(result) == expected and result["traceId"] == span["traceId"],
            f"invalid {expected} parent hierarchy")
    require(all(span["attributes"].get(k) == result["attributes"].get(k) for k in IDENTITY),
            "parent identity mismatch")
    return result


def query_spans(spans: dict, *, reference: int, top: int = 10) -> list[dict]:
    """Apply the archive contract to normalized, deduplicated complete trees."""
    require(type(top) is int and top > 0, "top must be a positive integer")
    counts = Counter()

    for span, location in spans.values():
        try:
            kind = role(span)
            if kind is None:
                continue
            attrs = span["attributes"]
            require(all(isinstance(attrs.get(k), str) and attrs[k].strip() for k in IDENTITY),
                    "missing explicit Graph/Run/Subject identity; legacy archives require re-emission")
            if kind == "run":
                require(not span.get("parentSpanId"), "Run must be a root span")
                require(attrs.get("cord.semconv.version") in ("0.2.0", SEMCONV_VERSION),
                        "unsupported semantic convention version; re-emit with current instrumentation")
                continue
            require(isinstance(attrs.get("cord.node.name"), str) and attrs["cord.node.name"].strip(),
                    "missing Node identity")
            if kind == "step":
                require(attrs.get("cord.outcome") in tuple(x.value for x in StepOutcome), "invalid Step outcome")
                parent(spans, span, "run")
                continue
            require(type(attrs.get("cord.step.attempt")) is int and attrs["cord.step.attempt"] > 0,
                    "Attempt requires a positive attempt number")
            require(attrs.get("cord.outcome") in tuple(x.value for x in AttemptOutcome), "invalid Attempt outcome")
            step = parent(spans, span, "step")
            root = parent(spans, step, "run")
            # Historical 0.2 records required Tier. New graphs need not use or
            # disclose models; any supplied Tier remains an opaque annotation.
            if root["attributes"].get("cord.semconv.version") == "0.2.0" or "cord.tier" in attrs:
                require(isinstance(attrs.get("cord.tier"), str) and attrs["cord.tier"].strip(),
                        "missing or invalid Attempt Tier")
            require(attrs["cord.node.name"] == step["attributes"].get("cord.node.name"), "parent Node identity mismatch")
            if (attrs["cord.outcome"] == "escalated"
                    and reference - WINDOW_NS < span["endTimeUnixNano"] <= reference):
                counts[attrs["cord.graph.id"], attrs["cord.node.name"]] += 1
        except ArchiveError as exc:
            raise ArchiveError(f"{location}: {exc}") from None
    return [{"graph": graph, "node": node, "count": count}
            for (graph, node), count in sorted(counts.items(), key=lambda item: (-item[1], *item[0]))[:top]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--reference-time", required=True, type=reference_ns)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    try:
        rows = query(args.paths, reference=args.reference_time, top=args.top)
    except ArchiveError as exc:
        print(f"archive-escalations: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
