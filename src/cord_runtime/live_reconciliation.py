"""Reconcile live Aegra execution with recorded archive/Langfuse execution (#20).

Consumes only the public identities and lifecycle status Aegra's SSE stream
exposes (`cord_runtime.aegra_client.stream_lifecycle`/`watch_lifecycle`): a
Run's own id, its Aegra status (`pending`/`running`/`interrupted`/`success`/
`error`/`timeout`), and the monotonic per-Run event id Aegra assigns each
lifecycle frame. No graph business state (`values`/`updates`/`messages*`/
`debug` events) is read here or anywhere upstream of this module, so a
model-free graph's Run reconciles identically to one with Tier telemetry
(#20 AC1).
"""

LIVE = "live"
INGESTION_PENDING = "ingestion_pending"
INGESTION_FAILED = "ingestion_failed"

# Aegra's own terminal Run statuses (models/runs.py): the Run has stopped
# producing new lifecycle events. `interrupted` is a pause (ADR-0008's
# awaiting_approval), not a failure.
TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})

DEFAULT_INGESTION_PENDING_AFTER = 5.0
DEFAULT_INGESTION_FAILED_AFTER = 300.0


class LiveRun:
    """One Run's reconciled live state, keyed by its Aegra ``run_id`` (or the
    logical Run id a caller substitutes via #14's ``run_continuity`` for a
    Run whose Aegra ``run_id`` changed across a resume)."""

    def __init__(self, run_id: str, thread_id: str, *, graph_id: str | None = None,
                 assistant_id: str | None = None, subject: str | None = None):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        self.run_id = run_id
        self.thread_id = thread_id
        self.graph_id = graph_id
        self.assistant_id = assistant_id
        self.subject = subject
        self.status = "pending"
        self.last_event_seq = -1  # no event applied yet; Aegra's own sequences start at 0
        self.completed_at: float | None = None

    def apply(self, event: str, data: dict, event_id: str | None, *, now: float) -> None:
        """Idempotently fold one lifecycle event into this Run's state.

        A replayed or duplicate event (sequence no higher than the last one
        already applied) never regresses ``status`` or ``completed_at`` --
        #20 AC3's no-duplication/no-lost-progress guarantee, resting on
        Aegra's own monotonic per-Run event ids
        (``{run_id}_event_{sequence}``). An event with no id (not expected
        from Aegra's lifecycle events, but not assumed impossible) is always
        applied since it carries nothing to dedup against.
        """
        if event_id is not None:
            seq = _sequence(event_id)
            if seq <= self.last_event_seq:
                return
            self.last_event_seq = seq
        if event == "end":
            self.status = data.get("status") or "success"
            if self.completed_at is None:
                self.completed_at = now
        elif event == "error":
            self.status = "error"
            if self.completed_at is None:
                self.completed_at = now
        elif event == "metadata" and self.status == "pending":
            self.status = "running"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _sequence(event_id: str | None) -> int:
    if not event_id:
        return 0
    try:
        return int(event_id.rsplit("_event_", 1)[-1])
    except ValueError:
        return 0


def reconcile(live_runs: dict[str, LiveRun], recorded_run_ids, *, now: float,
              ingestion_pending_after: float = DEFAULT_INGESTION_PENDING_AFTER,
              ingestion_failed_after: float = DEFAULT_INGESTION_FAILED_AFTER) -> dict[str, dict]:
    """Project ``live_runs`` into the display facts the viewer renders.

    A Run whose ``run_id`` already appears in ``recorded_run_ids`` is
    dropped entirely: the durable record always wins over the temporary
    live one (#20 AC4). A still-running or just-completed Run is ``LIVE``;
    past ``ingestion_pending_after`` seconds since its live completion with
    no recorded counterpart it becomes ``INGESTION_PENDING`` (still visible,
    #20 AC2); past ``ingestion_failed_after`` it becomes ``INGESTION_FAILED``
    -- a bounded, visible diagnostic rather than an endless loading state
    (#20 AC5).
    """
    recorded = set(recorded_run_ids)
    views = {}
    for run_id, live in live_runs.items():
        if run_id in recorded:
            continue
        if live.completed_at is None:
            source, seconds = LIVE, None
        else:
            seconds = max(0.0, now - live.completed_at)
            if seconds >= ingestion_failed_after:
                source = INGESTION_FAILED
            elif seconds >= ingestion_pending_after:
                source = INGESTION_PENDING
            else:
                source = LIVE
        views[run_id] = {
            "run_id": run_id,
            "thread_id": live.thread_id,
            "graph_id": live.graph_id,
            "assistant_id": live.assistant_id,
            "subject": live.subject,
            "aegra_status": live.status,
            "source": source,
            "seconds_since_completion": seconds,
        }
    return views


def await_convergence(live_runs: dict[str, LiveRun], get_recorded_run_ids, *, now_fn, sleep_fn,
                       poll_interval: float = 1.0,
                       ingestion_pending_after: float = DEFAULT_INGESTION_PENDING_AFTER,
                       ingestion_failed_after: float = DEFAULT_INGESTION_FAILED_AFTER):
    """Re-poll the archive contract until every completed Run in ``live_runs``
    is replaced by its durable record or reaches the bounded
    ``ingestion_failed_after`` diagnostic (#46 AC3/AC4), yielding the
    reconciled view dict after every poll (the first included) so a caller
    can render convergence progress rather than only a final snapshot.

    ``get_recorded_run_ids`` is called fresh on every poll (the archive is
    re-read, not read once) and ``now_fn``/``sleep_fn`` are injected so the
    thresholds above are exercisable with a controlled clock instead of real
    waiting. A Run that never completed (``completed_at`` is still ``None``
    when its watch ended, e.g. a `--watch-timeout` gave up while it was still
    running) is not polled for -- there is nothing to converge toward yet,
    and #46 never blocks on a Run that is still genuinely live.
    """
    while True:
        views = reconcile(live_runs, get_recorded_run_ids(), now=now_fn(),
                           ingestion_pending_after=ingestion_pending_after,
                           ingestion_failed_after=ingestion_failed_after)
        yield views
        unresolved = [run_id for run_id, live in live_runs.items()
                      if live.completed_at is not None and run_id in views
                      and views[run_id]["source"] != INGESTION_FAILED]
        if not unresolved:
            return
        sleep_fn(poll_interval)
