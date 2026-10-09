import json
from concurrent.futures import ThreadPoolExecutor

from morphx.models import Record
from morphx.server.storage import Storage


def fetch(client, device="MORPHX_SIM_001", **params):
    return client.get(f"/v1/devices/{device}/records", params=params).json()


def test_new_record_is_created_and_a_retry_is_a_harmless_duplicate(client, make_record):
    record = make_record()
    first = client.post("/v1/records", json=record)
    again = client.post("/v1/records", json=record)

    assert (first.status_code, first.json()["status"]) == (201, "created")
    assert (again.status_code, again.json()["status"]) == (200, "duplicate")
    assert len(fetch(client)["items"]) == 1


def test_receipt_time_is_kept_separately_and_set_by_the_server(client, make_record):
    client.post("/v1/records", json=make_record(measured_at="2020-01-01T00:00:00Z"))
    client.post(
        "/v1/records", json=make_record(sequence=2, received_at="1999-01-01T00:00:00Z")
    )  # ignored

    first, second = fetch(client)["items"]
    assert first["measured_at"] == "2020-01-01T00:00:00.000Z"
    assert first["received_at"] > first["measured_at"]
    assert not second["received_at"].startswith("1999")


def test_retry_does_not_change_the_original_receipt_time(client, make_record):
    record = make_record()
    client.post("/v1/records", json=record)
    before = fetch(client)["items"][0]["received_at"]
    client.post("/v1/records", json=record)
    assert fetch(client)["items"][0]["received_at"] == before


def test_different_record_with_a_taken_sequence_is_a_conflict(client, make_record):
    client.post("/v1/records", json=make_record(sequence=1))
    other = client.post("/v1/records", json=make_record(sequence=1, sample_id="SMP-OTHER"))
    assert other.status_code == 409
    assert len(fetch(client)["items"]) == 1


def test_invalid_record_is_refused_and_not_stored(client, make_record):
    record = make_record()
    record["measurements"]["hb"]["unit"] = "g/L"
    assert client.post("/v1/records", json=record).status_code == 422
    assert (
        client.post(
            "/v1/records", content=b"{broken", headers={"content-type": "application/json"}
        ).status_code
        == 422
    )
    assert fetch(client)["items"] == []


def test_records_come_back_in_acquisition_order_not_arrival_order(client, make_record):
    for sequence in (3, 1, 5, 2, 4):
        client.post("/v1/records", json=make_record(sequence=sequence))
    assert [r["sequence"] for r in fetch(client)["items"]] == [1, 2, 3, 4, 5]


def test_pagination_walks_through_every_record_once(client, make_record):
    for sequence in range(1, 8):
        client.post("/v1/records", json=make_record(sequence=sequence))

    seen, cursor = [], 0
    while page := fetch(client, limit=3, after_sequence=cursor):
        if not page["items"]:
            break
        seen += [r["sequence"] for r in page["items"]]
        cursor = page["next_after_sequence"]
    assert seen == [1, 2, 3, 4, 5, 6, 7]


def test_each_device_only_sees_its_own_records(client, make_record):
    client.post("/v1/records", json=make_record(sequence=1, device_id="DEV_A"))
    client.post("/v1/records", json=make_record(sequence=1, device_id="DEV_B"))
    client.post("/v1/records", json=make_record(sequence=2, device_id="DEV_A"))

    assert [r["sequence"] for r in fetch(client, "DEV_A")["items"]] == [1, 2]
    assert [r["sequence"] for r in fetch(client, "DEV_B")["items"]] == [1]
    assert fetch(client, "UNKNOWN")["items"] == []


def test_data_survives_a_server_restart(tmp_path, make_record):
    path = tmp_path / "server.sqlite3"
    Storage(path).add(Record.model_validate(make_record(sequence=1)))
    restarted = Storage(path)
    assert [r["sequence"] for r in restarted.for_device("MORPHX_SIM_001", 0, 10)] == [1]


def test_simultaneous_retries_store_exactly_one_row(tmp_path, make_record):
    storage = Storage(tmp_path / "server.sqlite3")
    record = Record.model_validate(make_record())
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: storage.add(record), range(16)))
    assert results.count(True) == 1
    assert len(storage.for_device("MORPHX_SIM_001", 0, 100)) == 1


def test_units_must_be_given(client, make_record):
    record = make_record()
    del record["measurements"]["wbc"]["unit"]
    assert client.post("/v1/records", json=record).status_code == 422


def test_nan_and_oversized_numbers_get_a_422_not_a_crash(client, make_record):
    body = json.dumps(make_record()).replace('"value": ', '"value": NaN, "x": ', 1)
    reply = client.post("/v1/records", content=body, headers={"content-type": "application/json"})
    assert reply.status_code == 422
    assert client.get("/v1/devices/D/records", params={"after_sequence": 2**64}).status_code == 422


def test_dashboard_page_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "morphx" in page.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200
    assert client.get("/ui/../storage.py").status_code in (404, 400)


def test_devices_summary_counts_and_missing_sequences(client, make_record):
    for sequence in (1, 2, 5):
        client.post("/v1/records", json=make_record(sequence=sequence))
    client.post("/v1/records", json=make_record(sequence=1, device_id="DEV_B"))

    devices = {d["device_id"]: d for d in client.get("/v1/devices").json()["devices"]}
    assert devices["MORPHX_SIM_001"]["count"] == 3
    assert devices["MORPHX_SIM_001"]["last_sequence"] == 5
    assert devices["MORPHX_SIM_001"]["missing"] == 2  # sequences 3 and 4 never arrived
    assert devices["DEV_B"]["missing"] == 0


def test_recent_is_in_arrival_order_and_exposes_it(client, make_record):
    for sequence in (3, 1, 2):
        client.post("/v1/records", json=make_record(sequence=sequence))
    items = client.get("/v1/recent").json()["items"]
    assert [i["sequence"] for i in items] == [2, 1, 3]  # newest arrival first
    assert [i["arrival"] for i in items] == [3, 2, 1]
    assert [i["sequence"] for i in client.get("/v1/recent?limit=1").json()["items"]] == [2]
    assert client.get("/v1/recent?device_id=NOPE").json()["items"] == []


def test_info_counts_what_happened_to_uploads(client, make_record):
    record = make_record()
    client.post("/v1/records", json=record)
    client.post("/v1/records", json=record)
    client.post("/v1/records", json=make_record(sequence=1, sample_id="SMP-OTHER"))
    client.post("/v1/records", json={"nonsense": True})
    counters = client.get("/v1/info").json()["counters"]
    assert counters == {"created": 1, "duplicate": 1, "conflict": 1, "invalid": 1, "unavailable": 0}


def test_demo_outage_refuses_uploads_until_it_ends(tmp_path, make_record):
    from fastapi.testclient import TestClient

    from morphx.server.app import create_app

    demo = TestClient(create_app(tmp_path / "demo.sqlite3", demo=True))
    assert demo.get("/v1/info").json()["demo"] is True
    demo.post("/v1/demo/outage?seconds=30")
    refused = demo.post("/v1/records", json=make_record())
    assert refused.status_code == 503 and demo.get("/v1/info").json()["outage_remaining"] > 0
    assert demo.get("/v1/devices").status_code == 200  # reads keep working
    demo.delete("/v1/demo/outage")
    assert demo.post("/v1/records", json=make_record()).status_code == 201
    assert demo.get("/v1/info").json()["counters"]["unavailable"] == 1


def test_outage_controls_do_not_exist_without_demo_mode(client):
    assert client.post("/v1/demo/outage").status_code in (404, 405)
    assert client.get("/v1/info").json()["demo"] is False


def test_outage_buttons_refuse_requests_from_other_websites(tmp_path):
    from fastapi.testclient import TestClient

    from morphx.server.app import create_app

    demo = TestClient(create_app(tmp_path / "demo.sqlite3", demo=True))
    other_site = {"Origin": "http://evil.example"}
    assert demo.post("/v1/demo/outage", headers=other_site).status_code == 403
    assert demo.delete("/v1/demo/outage", headers=other_site).status_code == 403
    assert demo.get("/v1/info").json()["outage_remaining"] == 0
    same_site = {"Origin": "http://testserver"}  # what the dashboard's own requests carry
    assert demo.post("/v1/demo/outage?seconds=5", headers=same_site).status_code == 200
