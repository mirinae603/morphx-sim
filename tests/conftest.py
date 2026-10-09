import random

import pytest
from fastapi.testclient import TestClient

from morphx.agent.generator import new_sample
from morphx.agent.outbox import Outbox
from morphx.server.app import create_app


@pytest.fixture
def make_record():
    """Build a record in the form the agent sends it."""

    def make(sequence=1, device_id="MORPHX_SIM_001", **overrides):
        sample = new_sample(random.Random(sequence))
        return {**sample, "device_id": device_id, "sequence": sequence, **overrides}

    return make


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "server.sqlite3"))


@pytest.fixture
def outbox(tmp_path):
    return Outbox(tmp_path / "agent.sqlite3", "MORPHX_SIM_001")
