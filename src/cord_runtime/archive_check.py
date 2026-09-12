"""Check raw and decoded OTLP JSONL with the pinned upstream CLI."""

import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import subprocess


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            if key == "bytesValue":
                yield base64.b64decode(item, validate=True).decode("utf-8")
            else:
                yield from strings(item)


def check(paths: list[Path], cli: str) -> int:
    found = False
    failed = False
    records = 0
    try:
        version = subprocess.run([cli, "--version"], capture_output=True, timeout=10)
        if version.returncode or version.stdout.strip() != b"redact-secret 0.1.0-beta.1":
            raise ValueError
        files = []
        for path in paths:
            if path.is_dir():
                entries = sorted(path.glob("*.otlp.jsonl")) + sorted(path.glob("*.otlp.jsonl.gz"))
                if not entries:
                    failed = True
                files.extend(entries)
            else:
                files.append(path)
        for path in files:
            try:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        record = json.loads(line)
                        if not isinstance(record, dict) or "resourceSpans" not in record:
                            raise ValueError
                        # JSON escaping and AnyValue base64 must not hide text.
                        decoded = "\n".join(strings(record))
                        result = subprocess.run(
                            [cli, "--json"], input=line + "\n" + decoded,
                            text=True, capture_output=True, timeout=30,
                        )
                        found |= result.returncode == 1
                        failed |= result.returncode not in (0, 1)
                        records += 1
            except (OSError, ValueError, TypeError, RecursionError, EOFError, subprocess.SubprocessError):
                failed = True
    except (OSError, ValueError, subprocess.SubprocessError):
        failed = True
    if not records:
        failed = True
    code = 2 if failed else 1 if found else 0
    print(f"archive-check: records={records} findings={found} failure={failed}")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--cli", default=os.environ.get("REDACT_SECRET_CLI", "redact-secret"))
    args = parser.parse_args()
    return check(args.paths, args.cli)


if __name__ == "__main__":
    raise SystemExit(main())
