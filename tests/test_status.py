import json
import urllib.request

from morphx.agent.status import SyncStatus, serve_status


def test_status_page_serves_json_with_cors():
    status = SyncStatus()
    status.failed("ConnectError: refused", retry_in=5)
    server = serve_status(0, lambda: {"online": status.online, "retry_in": status.retry_in()})
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/status"
        with urllib.request.urlopen(url, timeout=3) as reply:
            body = json.load(reply)
            assert reply.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.shutdown()
    assert body["online"] is False and 0 < body["retry_in"] <= 5


def test_status_tracks_success_and_failure():
    status = SyncStatus()
    assert status.online is None
    status.failed("down", retry_in=3)
    assert (status.online, status.last_error) == (False, "down")
    status.delivered("2026-10-09T10:00:00.000Z")
    assert status.online is True and status.last_error == "" and status.retry_in() == 0
