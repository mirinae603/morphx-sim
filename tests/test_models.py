import pytest
from pydantic import ValidationError

from morphx.models import Record, utc_iso


def test_valid_record_keeps_explicit_units_and_serialises_utc(make_record):
    record = Record.model_validate(make_record())
    data = record.model_dump(mode="json")
    assert utc_iso(record.measured_at).endswith("Z")
    units = {name: m["unit"] for name, m in data["measurements"].items()}
    assert units == {"wbc": "10^3/uL", "rbc": "10^6/uL", "hb": "g/dL"}


def test_timestamp_without_timezone_is_rejected(make_record):
    with pytest.raises(ValidationError):
        Record.model_validate(make_record(measured_at="2026-10-09T10:00:00"))


def test_other_timezones_are_converted_to_utc(make_record):
    record = Record.model_validate(make_record(measured_at="2026-10-09T15:30:00+05:30"))
    assert utc_iso(record.measured_at) == "2026-10-09T10:00:00.000Z"


@pytest.mark.parametrize("unit", ["10^6/uL", "g/dL", ""])
def test_wrong_unit_is_rejected(make_record, unit):
    record = make_record()
    record["measurements"]["wbc"]["unit"] = unit
    with pytest.raises(ValidationError):
        Record.model_validate(record)


@pytest.mark.parametrize(
    "bad",
    [
        {"sequence": 0},
        {"sequence": True},
        {"sequence": "7"},
        {"event_id": "nope"},
        {"device_id": "has space"},
        {"sample_id": ""},
        {"measured_at": "9999-12-31T23:59:59-05:00"},
        {"measured_at": "0001-01-01T00:00:00+05:30"},
    ],
)
def test_bad_fields_are_rejected(make_record, bad):
    with pytest.raises(ValidationError):
        Record.model_validate(make_record(**bad))
