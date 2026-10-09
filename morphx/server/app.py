"""The central server's HTTP API, plus the dashboard that is served at /."""

import threading
import time
from collections import Counter
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as UrlPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from morphx.models import ID_PATTERN, Record, utc_iso
from morphx.server.storage import SequenceConflict, Storage

STATIC = Path(__file__).parent / "static"
DeviceId = Annotated[str, UrlPath(pattern=ID_PATTERN)]


def same_origin_only(request: Request) -> None:
    """Refuse requests that a browser sends on behalf of some other website."""
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "cross-site request refused")


def create_app(db_path: str | Path, demo: bool = False) -> FastAPI:
    app = FastAPI(title="MorphX central server", version="1.0.0")
    storage = Storage(db_path)

    started = time.time()
    started_at = utc_iso()
    counters: Counter = Counter()  # what happened to uploads since this server started
    lock = threading.Lock()
    outage = {"until": 0.0}  # demo mode only: refuse uploads until this monotonic time

    def count(name: str) -> None:
        with lock:
            counters[name] += 1

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError):
        # The default handler echoes the bad input back, which fails for NaN. Leave it out.
        count("invalid")
        problems = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": problems})

    @app.post("/v1/records", status_code=201)
    def receive(record: Record, response: Response):
        """Store a record. Posting the same one again is harmless and answers 200 `duplicate`."""
        if time.monotonic() < outage["until"]:
            count("unavailable")
            raise HTTPException(503, "simulated outage", headers={"Retry-After": "2"})
        try:
            created = storage.add(record)
        except SequenceConflict as exc:
            count("conflict")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        count("created" if created else "duplicate")
        if not created:
            response.status_code = 200
        return {
            "status": "created" if created else "duplicate",
            "event_id": str(record.event_id),
        }

    @app.get("/v1/devices/{device_id}/records")
    def read(
        device_id: DeviceId,
        after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        """A device's records in acquisition order. Use `next_after_sequence` for the next page."""
        items = storage.for_device(device_id, after_sequence, limit)
        return {
            "device_id": device_id,
            "items": items,
            "next_after_sequence": items[-1]["sequence"] if items else after_sequence,
        }

    @app.get("/v1/devices")
    def devices():
        """Every device that has reported, with counts and the range of sequences held."""
        return {"devices": storage.devices()}

    @app.get("/v1/recent")
    def recent(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        device_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
    ):
        """The latest records in arrival order, newest first. `arrival` is the order received."""
        return {"items": storage.recent(limit, device_id)}

    @app.get("/v1/info")
    def info():
        with lock:
            seen = dict(counters)
        return {
            "version": app.version,
            "started_at": started_at,
            "now": utc_iso(),
            "uptime_s": round(time.time() - started, 1),
            "demo": demo,
            "outage_remaining": round(max(0.0, outage["until"] - time.monotonic()), 1),
            "counters": {
                key: seen.get(key, 0)
                for key in ("created", "duplicate", "conflict", "invalid", "unavailable")
            },
        }

    if demo:

        @app.post("/v1/demo/outage", dependencies=[Depends(same_origin_only)])
        def start_outage(seconds: Annotated[float, Query(gt=0, le=300)] = 15):
            """Demo mode: answer 503 to uploads for a while, as if the server were down."""
            outage["until"] = time.monotonic() + seconds
            return {"outage_remaining": seconds}

        @app.delete("/v1/demo/outage", dependencies=[Depends(same_origin_only)])
        def end_outage():
            outage["until"] = 0.0
            return {"outage_remaining": 0}

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    app.mount("/ui", StaticFiles(directory=STATIC), name="ui")
    return app
