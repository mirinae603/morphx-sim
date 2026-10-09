"""Uploads stored measurements to the server in the background."""

import logging
import random
import threading

import httpx

from morphx.agent.outbox import Outbox
from morphx.agent.status import SyncStatus
from morphx.models import utc_iso

log = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 1.0
DELIVERED, REJECTED, RETRY = "delivered", "rejected", "retry"


def _acknowledges(response: httpx.Response, record: dict) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("event_id") == record["event_id"]


def deliver(client: httpx.Client, record: dict) -> tuple[str, str]:
    """Send one record. Returns what to do next and a short reason."""
    try:
        response = client.post("/v1/records", json=record)
    except httpx.HTTPError as exc:
        return RETRY, f"{type(exc).__name__}: {exc}"

    code = response.status_code
    # 201 means stored, 200 means the server already had it (a retry). Safe either way,
    # as long as the reply really is the server's (not a proxy or captive portal page).
    if code in (200, 201):
        if _acknowledges(response, record):
            return DELIVERED, ""
        return RETRY, f"HTTP {code} but not an acknowledgement from the server"
    # The server will never accept these, so there is no point trying again.
    if code in (409, 422):
        return REJECTED, f"HTTP {code}: {response.text[:200]}"
    return RETRY, f"HTTP {code}"


def sync_forever(
    outbox: Outbox,
    client: httpx.Client,
    stop: threading.Event,
    max_backoff: float = 30.0,
    status: SyncStatus | None = None,
) -> None:
    """Send pending records, oldest first, until `stop` is set."""
    status = status or SyncStatus()
    delay = 0.0
    offline = False
    while not stop.is_set():
        try:
            record = outbox.oldest_pending()
            if record is None:
                stop.wait(IDLE_POLL_SECONDS)
                continue

            sequence = record["sequence"]
            result, detail = deliver(client, record)

            if result == RETRY:
                offline = True
                delay = min(max_backoff, max(1.0, delay * 2))
                log.warning(
                    "server unavailable (%s), %d waiting, retrying in up to %.0fs",
                    detail,
                    outbox.counts()["pending"],
                    delay,
                )
                wait = random.uniform(delay / 2, delay)  # jitter so devices don't sync up
                status.failed(detail, wait)
                stop.wait(wait)
                continue

            if offline:
                log.info("server reachable again, sending backlog")
                offline = False
            delay = 0.0

            if result == DELIVERED:
                outbox.mark(sequence, "synced")
                status.delivered(utc_iso())
                log.info("synced sequence %d", sequence)
            else:
                outbox.mark(sequence, "rejected", detail)
                log.error("sequence %d rejected, kept locally: %s", sequence, detail)
        except Exception:
            # Whatever went wrong (a locked database, say), this thread must not die:
            # measuring would carry on and nothing would ever be sent again.
            log.exception("sync step failed, trying again shortly")
            stop.wait(IDLE_POLL_SECONDS)
