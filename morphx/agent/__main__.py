"""Run the simulated device: python -m morphx.agent"""

import argparse
import logging
import math
import random
import re
import signal
import sqlite3
import threading
import time
from pathlib import Path

import httpx

from morphx.agent.generator import new_sample
from morphx.agent.outbox import Outbox
from morphx.agent.status import SyncStatus, serve_status
from morphx.agent.sync import sync_forever
from morphx.models import ID_PATTERN, utc_iso

log = logging.getLogger("morphx.agent")


def positive_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a number greater than 0")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="morphx-agent", description=__doc__)
    parser.add_argument("--device-id", default="MORPHX_SIM_001", help="name this device reports as")
    parser.add_argument(
        "--interval", type=positive_number, default=5.0, help="seconds between tests"
    )
    parser.add_argument(
        "--server-url", default="http://127.0.0.1:8000", help="where the server listens"
    )
    parser.add_argument("--db", help="local database [data/<device-id>.sqlite3]")
    parser.add_argument(
        "--max-backoff", type=positive_number, default=30.0, help="longest retry wait (s)"
    )
    parser.add_argument(
        "--request-timeout", type=positive_number, default=10.0, help="network timeout (s)"
    )
    parser.add_argument(
        "--status-port", type=int, default=0, help="serve a JSON status page here (0 = off)"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(ID_PATTERN, args.device_id):
        parser.error("--device-id may only contain letters, digits, '_', '.' and '-' (max 64)")
    args.db = args.db or f"data/{args.device_id}.sqlite3"
    return args


def acquire(outbox: Outbox, interval: float, stop: threading.Event) -> None:
    """Measure every `interval` seconds and save it locally. Nothing here touches the network."""
    rng = random.Random()
    next_tick = time.monotonic()
    while not stop.is_set():
        sample = new_sample(rng)
        try:
            sequence = outbox.add(sample)
        except sqlite3.Error:
            log.exception("could not store measurement")
        else:
            log.info("measured sequence %d (%s)", sequence, sample["sample_id"])

        next_tick += interval
        now = time.monotonic()
        if (
            next_tick < now
        ):  # we were stalled; drop the missed ticks rather than catch up in a burst
            next_tick = now + interval
        stop.wait(next_tick - now)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(threadName)s %(message)s"
    )
    logging.Formatter.converter = time.gmtime
    logging.getLogger("httpx").setLevel(logging.WARNING)

    outbox = Outbox(args.db, args.device_id)
    log.info(
        "%s starting, every %gs, database %s, outbox %s",
        args.device_id,
        args.interval,
        Path(args.db).resolve(),
        outbox.counts(),
    )

    status = SyncStatus()
    started = time.monotonic()
    if args.status_port:

        def payload() -> dict:
            return {
                "device_id": args.device_id,
                "interval": args.interval,
                "server_url": args.server_url,
                "database": str(Path(args.db).resolve()),
                "uptime_s": round(time.monotonic() - started, 1),
                "outbox": outbox.counts(),
                "online": status.online,
                "last_error": status.last_error,
                "retry_in": status.retry_in(),
                "last_synced_at": status.last_synced_at,
                "now": utc_iso(),
            }

        serve_status(args.status_port, payload)
        log.info("status page on http://127.0.0.1:%d/status", args.status_port)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    timeout = httpx.Timeout(args.request_timeout, connect=3.0)
    with httpx.Client(base_url=args.server_url, timeout=timeout) as client:
        syncer = threading.Thread(
            target=sync_forever,
            args=(outbox, client, stop, args.max_backoff, status),
            name="sync",
        )
        syncer.start()
        try:
            acquire(outbox, args.interval, stop)
        finally:
            stop.set()
            syncer.join()
    log.info("stopped, outbox %s", outbox.counts())


if __name__ == "__main__":
    main()
