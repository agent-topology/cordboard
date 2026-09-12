"""Compare a closed archive cohort through Langfuse 3.225.7's public v1 API."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import requests

from cord_runtime.archive_query import (
    ArchiveError, identifier, query_spans, read_spans, reference_ns, require, timestamp,
)


class ReadError(Exception):
    """Fixed status only: never include response bodies, URLs or credentials."""


class Pending(Exception):
    pass


def projection(span):
    """Compare execution metadata and identity, excluding vendor enrichment."""
    return {key: span.get(key) or None for key in (
        'spanId', 'traceId', 'parentSpanId', 'name', 'startTimeUnixNano', 'endTimeUnixNano'
    )} | {'attributes': {k: v for k, v in span['attributes'].items() if k.startswith('cord.')}}


def observation(row):
    """Decode the explicit public OTLP metadata mapping; never supplement it from archive."""
    try:
        metadata = row['metadata']
        attrs = metadata['cordboard']
        require(isinstance(attrs, dict), 'invalid public attributes')
        if row.get('endTime') is None:
            raise Pending
        start, end = timestamp(metadata['cord_start_ns']), timestamp(metadata['cord_end_ns'])
        require(end >= start, 'invalid public time range')
        for native, precise in ((row['startTime'], start), (row['endTime'], end)):
            require(abs(reference_ns(native) - precise) < 1_000_000, 'public timestamp mismatch')
        return {
            'spanId': identifier(row['id'], 16), 'traceId': identifier(row['traceId'], 32),
            'parentSpanId': identifier(row['parentObservationId'], 16) if row.get('parentObservationId') else None,
            'name': row['name'], 'startTimeUnixNano': start, 'endTimeUnixNano': end,
            'attributes': {k: v for k, v in attrs.items() if k.startswith('cord.')},
        }
    except (KeyError, TypeError, ValueError, AttributeError, argparse.ArgumentTypeError):
        raise ReadError('invalid_response') from None


class PublicReader:
    def __init__(self, url, public_key, secret_key):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.netloc or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')):
            raise ValueError('Langfuse URL must be an HTTP(S) origin without credentials')
        if not public_key or not secret_key:
            raise ValueError('Langfuse project keys are required')
        self.url = url.rstrip('/')
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (public_key, secret_key)

    def close(self):
        self.session.close()

    def get(self, path, params, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Pending
        try:
            response = self.session.get(self.url + '/api/public/' + path, params=params,
                                        timeout=min(5, remaining), allow_redirects=False)
        except requests.RequestException:
            raise ReadError('unavailable') from None
        if response.status_code == 404 and path.startswith(('traces/', 'observations/')):
            raise Pending
        if response.status_code == 429 or response.status_code >= 500:
            raise ReadError('unavailable')
        if response.status_code != 200:
            raise ReadError('api_error')
        try:
            return response.json()
        except ValueError:
            raise ReadError('invalid_response') from None

    def trace(self, trace_id, deadline):
        # No start-time filter: a long Attempt can finish inside the end-time window.
        found = {}
        page = 1
        while True:
            result = self.get('observations', {'traceId': trace_id, 'page': page, 'limit': 100}, deadline)
            try:
                rows, meta = result['data'], result['meta']
                total, pages = meta['totalItems'], meta['totalPages']
                if (not isinstance(rows, list) or type(total) is not int or total < 0
                        or type(pages) is not int or pages < 0
                        or type(meta['page']) is not int or meta['page'] != page):
                    raise ValueError
                for row in rows:
                    span = observation(row)
                    if span['traceId'] != trace_id:
                        raise ValueError
                    sid = span['spanId']
                    if sid in found and found[sid] != span:
                        raise ValueError
                    found[sid] = span
                if page >= pages:
                    if len(found) != total:
                        raise Pending  # pagination changed during ingestion; restart the read
                    break
                if not rows:
                    raise ValueError
                page += 1
            except (KeyError, TypeError, ValueError):
                raise ReadError('invalid_response') from None
        if not found:
            raise Pending
        trace = self.get('traces/' + trace_id, {}, deadline)
        try:
            if trace['id'] != trace_id or not isinstance(trace['sessionId'], str):
                raise ValueError
            subject = trace['sessionId']
        except (KeyError, TypeError, ValueError):
            raise ReadError('invalid_response') from None
        return found, subject


def compare(paths, reader, *, reference, top=10, wait=60, poll=1):
    """Freeze archive input, wait for all IDs, then compare full counts and fields.

    The cohort is exactly the complete traces in these archive partitions, not
    unrelated project traffic. This does not establish global ingestion health.
    """
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (wait, poll)):
        raise ValueError('wait and poll must be positive finite seconds')
    expected = read_spans(paths)
    # Validate all trees, including out-of-window spans, before any network call.
    all_expected = query_spans(expected, reference=reference, top=len(expected))
    require(type(top) is int and top > 0, 'top must be a positive integer')
    result = {'status': 'pending', 'archive': all_expected[:top], 'langfuse': None,
              'expected_spans': len(expected), 'observed_spans': 0}
    by_trace = {}
    for sid, (span, _) in expected.items():
        by_trace.setdefault(span['traceId'], set()).add(sid)
    deadline = time.monotonic() + wait
    last_status = 'timeout'
    while time.monotonic() < deadline:
        actual = {}
        subjects = {}
        complete = True
        same_ids = True
        try:
            for tid in sorted(by_trace):
                spans, subject = reader.trace(tid, deadline)
                complete = complete and by_trace[tid] <= set(spans)
                same_ids = same_ids and by_trace[tid] == set(spans)
                actual.update({sid: (s, 'public observation') for sid, s in spans.items()})
                subjects[tid] = subject
            result['observed_spans'] = len(actual)
            if not complete:
                raise Pending
            # Missing spans are pending; a complete but different cohort is a mismatch.
            same = same_ids and all(
                projection(s) == projection(actual[sid][0])
                and (s['attributes'].get('cord.subject.id') is None
                     or subjects[s['traceId']] == s['attributes']['cord.subject.id'])
                for sid, (s, _) in expected.items()
            )
            try:
                rows = query_spans(actual, reference=reference, top=len(actual))
            except ArchiveError:
                return result | {'status': 'mismatch'}
            return result | {'status': 'match' if same and rows == all_expected else 'mismatch',
                             'langfuse': rows[:top]}
        except Pending:
            last_status = 'timeout'
        except ReadError as exc:
            last_status = str(exc)
            if last_status != 'unavailable':
                return result | {'status': last_status}
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll, remaining))
    return result | {'status': last_status}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', type=Path, nargs='+')
    parser.add_argument('--reference-time', type=reference_ns, required=True)
    parser.add_argument('--top', type=int, default=10)
    parser.add_argument('--wait', type=float, default=60)
    parser.add_argument('--url', default=os.environ.get('CORD_LANGFUSE_URL', 'http://127.0.0.1:3300'))
    args = parser.parse_args()
    reader = None
    try:
        reader = PublicReader(args.url, os.environ.get('LANGFUSE_PUBLIC_KEY'), os.environ.get('LANGFUSE_SECRET_KEY'))
        result = compare(args.paths, reader, reference=args.reference_time, top=args.top, wait=args.wait)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'match' else 1
    except (ArchiveError, ValueError):
        print('comparison failed: invalid configuration or archive input', file=sys.stderr)
        return 2
    finally:
        if reader is not None:
            reader.close()


if __name__ == '__main__':
    raise SystemExit(main())
