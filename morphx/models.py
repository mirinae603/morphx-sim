"""The measurement record shared by the agent and the server."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
Identifier = Annotated[str, Field(pattern=ID_PATTERN)]

# Keeps timestamp arithmetic (and the database) away from absurd dates.
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
LATEST = datetime(2100, 1, 1, tzinfo=UTC)


def utc_iso(moment: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp with milliseconds, e.g. 2026-10-09T10:00:05.123Z."""
    moment = (moment or datetime.now(UTC)).astimezone(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The bounds are sanity checks (physically impossible values), not clinical reference ranges.
class Wbc(BaseModel):
    value: float = Field(strict=True, gt=0, le=1000, allow_inf_nan=False)
    unit: Literal["10^3/uL"]


class Rbc(BaseModel):
    value: float = Field(strict=True, gt=0, le=30, allow_inf_nan=False)
    unit: Literal["10^6/uL"]


class Hb(BaseModel):
    value: float = Field(strict=True, gt=0, le=40, allow_inf_nan=False)
    unit: Literal["g/dL"]


class Measurements(BaseModel):
    wbc: Wbc
    rbc: Rbc
    hb: Hb


class Record(BaseModel):
    """One simulated test, as it travels from the agent to the server."""

    event_id: UUID
    device_id: Identifier
    sample_id: Identifier
    sequence: int = Field(strict=True, ge=1, le=2**63 - 1)
    measured_at: AwareDatetime = Field(gt=EARLIEST, lt=LATEST)
    measurements: Measurements
