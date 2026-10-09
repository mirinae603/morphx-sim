import json
import sqlite3
import threading
import time

import httpx
import pytest

from morphx.agent.sync import DELIVERED, REJECTED, RETRY, deliver, sync_forever


def ack(request, status=201):
    """What the real server answers: the status plus the event_id it stored."""
    event_id = json.loads(request.content)["event_id"]
    return httpx.Response(status, json={"status": "created", "event_id": event_id})


def mock_client(handler):
    return httpx.Client(base_url="http://server", transport=httpx.MockTransport(handler))


def run_sync_until(outbox, client, done, timeout=10):
    """Run the real sync loop in a thread until `done()` is true."""
    stop = threading.Event()
    thread = threading.Thread(target=sync_forever, args=(outbox, client, stop, 0.1))
    thread.start()
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (201, DELIVERED),
        (200, DELIVERED),
        (409, REJECTED),
        (422, REJECTED),
        (500, RETRY),
        (503, RETRY),
        (429, RETRY),
        (404, RETRY),  # wrong URL or proxy trouble says nothing about the record
    ],
)
def test_what_each_server_answer_means(status, expected, make_record):
    client = mock_client(lambda request: ack(request, status))
    assert deliver(client, make_record())[0] == expected


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, text="<html>please log in to the wifi</html>"),
        httpx.Response(200, json={"status": "created", "event_id": "someone-else"}),
        httpx.Response(200, json=["not", "ours"]),
        httpx.Response(204),
    ],
)
def test_a_reply_that_is_not_the_servers_acknowledgement_does_not_count(reply, make_record):
    client = mock_client(lambda request: reply)
    assert deliver(client, make_record())[0] == RETRY


def test_network_failures_and_timeouts_are_retried(make_record):
    def down(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("no answer")

    assert deliver(mock_client(down), make_record())[0] == RETRY
    assert deliver(mock_client(slow), make_record())[0] == RETRY


def test_backlog_is_sent_in_order_once_the_server_comes_back(outbox, make_record):
    for _ in range(5):
        outbox.add(make_record())
    server = {"up": False, "received": []}

    def handler(request):
        if not server["up"]:
            raise httpx.ConnectError("refused")
        body = json.loads(request.content)
        server["received"].append(body["sequence"])
        return ack(request)

    stop = threading.Event()
    thread = threading.Thread(target=sync_forever, args=(outbox, mock_client(handler), stop, 0.1))
    thread.start()
    time.sleep(0.4)
    assert outbox.counts()["pending"] == 5  # nothing is marked as sent while the server is down
    server["up"] = True
    deadline = time.monotonic() + 5
    while outbox.counts()["pending"] and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join()

    assert server["received"] == [1, 2, 3, 4, 5]
    assert outbox.counts()["synced"] == 5


def test_a_rejected_record_does_not_block_the_ones_behind_it(outbox, make_record):
    for _ in range(3):
        outbox.add(make_record())

    def handler(request):
        sequence = json.loads(request.content)["sequence"]
        return ack(request, 422 if sequence == 2 else 201)

    run_sync_until(outbox, mock_client(handler), lambda: outbox.counts()["pending"] == 0)
    assert outbox.counts() == {"pending": 0, "synced": 2, "rejected": 1}


def test_resending_after_a_lost_acknowledgement_creates_no_duplicate(outbox, client, make_record):
    outbox.add(make_record())

    # The server stores the record, but the agent "crashes" before it can mark it as synced.
    assert deliver(client, outbox.oldest_pending())[0] == DELIVERED
    assert outbox.counts()["pending"] == 1

    # After the restart the record is still pending, so it is simply sent again.
    run_sync_until(outbox, client, lambda: outbox.counts()["pending"] == 0)
    items = client.get("/v1/devices/MORPHX_SIM_001/records").json()["items"]
    assert len(items) == 1
    assert outbox.counts()["synced"] == 1


def test_sync_carries_on_after_an_unexpected_error(outbox, make_record, monkeypatch):
    outbox.add(make_record())
    real = outbox.oldest_pending
    calls = []

    def locked_once():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(outbox, "oldest_pending", locked_once)
    client = mock_client(ack)
    run_sync_until(outbox, client, lambda: outbox.counts()["pending"] == 0)
    assert outbox.counts()["synced"] == 1
