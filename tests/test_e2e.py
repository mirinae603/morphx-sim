"""The real thing: separate server and agent processes, with outages and crashes."""

import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from morphx.agent.outbox import Outbox

DEVICE = "MORPHX_SIM_001"


def wait_for(condition, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def system(tmp_path):
    """Starts and stops real processes; everything is killed at the end of the test."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    processes = []

    class System:
        outbox = Outbox(tmp_path / "agent.sqlite3", DEVICE)

        def run(self, module, *args):
            with open(tmp_path / f"{len(processes)}-{module}.log", "wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", module, *args], stdout=log, stderr=subprocess.STDOUT
                )
            processes.append(process)
            return process

        def start_server(self):
            server = self.run("morphx.server", "--port", str(port), "--db", str(tmp_path / "s.db"))
            assert wait_for(lambda: httpx.get(f"{url}/healthz").status_code == 200)
            return server

        def start_agent(self):
            return self.run(
                "morphx.agent",
                *("--interval", "0.2", "--max-backoff", "1", "--request-timeout", "2"),
                *("--server-url", url, "--db", str(tmp_path / "agent.sqlite3")),
            )

        def stored(self):
            """Everything the server holds for the device, read page by page."""
            items, cursor = [], 0
            while True:
                page = httpx.get(
                    f"{url}/v1/devices/{DEVICE}/records",
                    params={"after_sequence": cursor, "limit": 50},
                ).json()
                if not page["items"]:
                    return items
                items += page["items"]
                cursor = page["next_after_sequence"]

        def drained(self):
            counts = self.outbox.counts()
            return counts["pending"] == 0 and len(self.stored()) == counts["synced"]

    yield System()
    for process in processes:
        process.send_signal(signal.SIGKILL)
        process.wait()


def test_outage_and_crashes_lose_nothing_and_duplicate_nothing(system):
    server = system.start_server()
    agent = system.start_agent()
    assert wait_for(lambda: len(system.stored()) >= 3), "records should flow to the server"

    # The server dies hard. The device keeps measuring and keeps the results locally.
    server.send_signal(signal.SIGKILL)
    server.wait()
    before = sum(system.outbox.counts().values())
    time.sleep(2.5)
    assert sum(system.outbox.counts().values()) >= before + 8
    assert system.outbox.counts()["pending"] >= 8
    assert agent.poll() is None

    # The server returns; the backlog arrives without anyone doing anything.
    system.start_server()
    assert wait_for(system.drained), "backlog should sync by itself"

    # The device crashes and restarts: the sequence carries on, nothing is repeated.
    agent.send_signal(signal.SIGKILL)
    agent.wait()
    total = sum(system.outbox.counts().values())
    system.start_agent()
    assert wait_for(lambda: sum(system.outbox.counts().values()) >= total + 3)
    assert wait_for(system.drained)

    records = system.stored()
    assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))
    assert len({r["event_id"] for r in records}) == len(records)
    assert system.outbox.counts()["rejected"] == 0
