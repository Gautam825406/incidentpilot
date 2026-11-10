"""In-process background worker for investigations (Phase 4).

Webhook-triggered investigations run off the request path: the webhook
handler creates the incident, enqueues its id, and returns immediately —
the response contract is unchanged. A single asyncio worker task drains
the queue, runs ``run_investigation`` with its own DB session, and posts
the best-effort Slack notification on the outcome (same behaviour as the
previous ``asyncio.create_task(_run_bg(...))`` wiring, now explicit and
observable via ``pending_count``).

``/api/simulate`` stays SYNCHRONOUS on purpose: its contract returns the
investigation inline in the response, so it cannot move to the queue
without breaking callers. That is the documented trade-off: simulate is
the interactive demo path; webhooks (the production-shaped path) are
async.

Scale-out path: replace this queue with Celery + Redis — the task
interface is the same (``enqueue_investigation(incident_id)``) — see
README "Background worker". The worker is single-process and in-memory:
queued ids are lost on restart, and it does not share work across
uvicorn workers. Acceptable for the demo; not a production job queue.
"""
import asyncio
import logging

from .agents import run_investigation
from .database import SessionLocal
from .integrations.slack import get_slack_client
from .models import Incident

logger = logging.getLogger(__name__)


async def notify_slack_best_effort(event: str, incident: Incident, hypothesis: dict | None = None):
    """Phase 3 — fire-and-forget Slack notification. Never raises and never
    blocks the incident flow; a no-op when Slack is not configured, and a
    failed post is swallowed the same way (the incident is the source of
    truth, not the notification)."""
    try:
        client = get_slack_client()
        if client is None:
            return
        await client.notify(event, incident, hypothesis=hypothesis)
    except Exception:
        pass


class InvestigationWorker:
    """One asyncio consumer task draining an in-memory queue of incident ids."""

    def __init__(self):
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def start(self) -> bool:
        """Start (or restart) the consumer on the current running loop.

        Idempotent. Rebinds when called on a different loop than the one
        the consumer task belongs to (e.g. a fresh TestClient portal), so
        a stale task from a dead loop never silently swallows the queue.
        Returns False when there is no running loop (nothing to bind to).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._task = loop.create_task(self._consume())
        return True

    def enqueue(self, incident_id: str) -> bool:
        """Queue an investigation. Returns False if no event loop is
        running (caller is not inside an async request handler) — the
        incident still exists and can be investigated via
        POST /api/incidents/{id}/investigate."""
        if not self.start():
            logger.warning("No running event loop — investigation for %s not queued", incident_id)
            return False
        self._queue.put_nowait(incident_id)
        return True

    async def _consume(self):
        while True:
            incident_id = await self._queue.get()
            try:
                await self._run_one(incident_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background investigation failed for %s", incident_id)
            finally:
                self._queue.task_done()

    async def _run_one(self, incident_id: str):
        db = SessionLocal()
        try:
            inc = db.query(Incident).filter_by(id=incident_id).first()
            if not inc:
                return
            result = await run_investigation(db, inc)
            if result.get("status") == "abstained":
                await notify_slack_best_effort("new_incident", inc)
            else:
                hyps = result.get("hypotheses") or []
                await notify_slack_best_effort("hypothesis_ready", inc,
                                               hypothesis=hyps[0] if hyps else None)
        finally:
            db.close()


# Module-level singleton: the webhook handlers enqueue here.
worker = InvestigationWorker()


def enqueue_investigation(incident_id: str) -> bool:
    """Queue a webhook-triggered investigation (see module docstring)."""
    return worker.enqueue(incident_id)
