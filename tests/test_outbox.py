import random

from morphx.agent.generator import new_sample
from morphx.agent.outbox import Outbox


def sample():
    return new_sample(random.Random())


def test_sequence_counts_up_from_one(outbox):
    assert [outbox.add(sample()) for _ in range(3)] == [1, 2, 3]


def test_sequence_and_identity_survive_a_restart(tmp_path):
    first = Outbox(tmp_path / "a.sqlite3", "D1")
    stored = sample()
    first.add(stored)
    sent_before = first.oldest_pending()

    restarted = Outbox(tmp_path / "a.sqlite3", "D1")
    assert restarted.oldest_pending() == sent_before  # same event_id, same values, same time
    assert sent_before["event_id"] == stored["event_id"]
    assert restarted.add(sample()) == 2


def test_oldest_pending_goes_in_sequence_order(outbox):
    for _ in range(3):
        outbox.add(sample())
    assert outbox.oldest_pending()["sequence"] == 1
    outbox.mark(1, "synced")
    outbox.mark(2, "rejected", "bad")
    assert outbox.oldest_pending()["sequence"] == 3
    outbox.mark(3, "synced")
    assert outbox.oldest_pending() is None
    assert outbox.counts() == {"pending": 0, "synced": 2, "rejected": 1}
