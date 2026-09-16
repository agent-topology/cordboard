"""Collector reachability, independent of Aegra/runtime status (#45 AC2).

`collector/config.yaml` enables the pinned Collector's own `health_check`
extension (`CORD_HEALTH_ENDPOINT`, default `127.0.0.1:13133`), a supported
operational interface distinct from the OTLP receiver a producer exports to.
Polling it answers "is the Collector itself up" without inferring that from
a graph Run's own success/failure, so a Collector outage is visible even
when nothing was exported during the outage.

Recovery is always bounded by an explicit `timeout`; nothing here can block
forever waiting for a Collector that never comes back.
"""

import time

import requests

REACHABLE = "reachable"
UNAVAILABLE = "unavailable"

DEFAULT_TIMEOUT = 5.0
DEFAULT_RECOVERY_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 0.5


def check_collector_health(endpoint: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """One bounded probe of the Collector's health endpoint.

    Only an HTTP 200 is `REACHABLE`; a connection failure, timeout, or any
    other status is `UNAVAILABLE` -- fixed vocabulary, never a response body
    or header (#45 AC5).
    """
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(endpoint, timeout=timeout, allow_redirects=False)
        return REACHABLE if response.status_code == 200 else UNAVAILABLE
    except requests.RequestException:
        return UNAVAILABLE


def wait_for_collector_recovery(endpoint: str, *, timeout: float = DEFAULT_RECOVERY_TIMEOUT,
                                 poll_interval: float = DEFAULT_POLL_INTERVAL) -> bool:
    """Poll `endpoint` until it is `REACHABLE` or `timeout` seconds elapse.

    Returns whether recovery was observed within the bound; a caller reports
    a timed-out wait as an explicit, visible diagnostic rather than looping
    forever or silently treating "not yet" as "never" (#45 AC2).
    """
    deadline = time.monotonic() + timeout
    while True:
        if check_collector_health(endpoint, timeout=min(poll_interval, timeout)) == REACHABLE:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)
