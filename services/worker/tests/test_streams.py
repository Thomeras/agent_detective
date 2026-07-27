"""Streams: message-envelope decoding, the dead-letter reaper, and the
orphaned-pending reclaim (Track C1)."""

import asyncio

from worker.streams import (
    DLQ_SUFFIX,
    _decode,
    reap_dead_letters,
    reclaim_pending_messages,
)
from worker.tier1 import Tier1Processor, run_tier1
from worker.types import (
    GROUP_TIER1,
    STREAM_GRAPHS_COMPLETED,
    PendingEntry,
    StreamMessage,
)

from conftest import (
    FakeJudge,
    FakeObjectStore,
    FakeRepo,
    FakeStreams,
    make_bundle,
    make_run,
    make_settings,
    uid,
)


class _StopAfter:
    """A stop token whose ``is_set`` returns False for the first ``n`` checks
    then True, so a consumer loop runs exactly ``n`` iterations deterministically
    (no sleeps, no races)."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._calls = 0

    def is_set(self) -> bool:
        self._calls += 1
        return self._calls > self._n


def test_decode_bytes_and_str_data_field():
    assert _decode({b"data": b'{"a": 1}'}) == {"a": 1}
    assert _decode({"data": '{"b": 2}'}) == {"b": 2}
    assert _decode({b"data": b"not json"}) == {}
    assert _decode({}) == {}


def test_reaper_moves_only_over_threshold_messages_to_dlq():
    streams = FakeStreams()
    stream = "ad.graphs.tier2"
    # One message under threshold, one over.
    streams.pending_rows[stream] = [
        PendingEntry(id="0-1", delivery_count=3),
        PendingEntry(id="0-2", delivery_count=6),
    ]

    moved = asyncio.run(
        reap_dead_letters(
            streams,
            streams,
            stream,
            "tier2",
            "reaper",
            max_deliveries=5,
            min_idle_ms=1000,
        )
    )
    assert moved == 1
    dlq = streams.messages(stream + DLQ_SUFFIX)
    assert len(dlq) == 1
    assert dlq[0]["original_id"] == "0-2"
    assert dlq[0]["delivery_count"] == 6
    # The poison message was acked on the source group.
    assert streams.acked[stream] == ["0-2"]


def test_reaper_no_op_when_nothing_pending():
    streams = FakeStreams()
    moved = asyncio.run(
        reap_dead_letters(streams, streams, "s", "g", "r", max_deliveries=5, min_idle_ms=1)
    )
    assert moved == 0
    assert streams.messages("s" + DLQ_SUFFIX) == []


def test_reclaim_returns_own_pending_but_leaves_poison_for_reaper():
    """Reclaim re-delivers stale entries still within the reaper's budget and
    leaves over-budget poison for the reaper to DLQ — the two loops partition
    the pending set on delivery count."""
    streams = FakeStreams()
    stream = "ad.graphs.completed"
    fresh = streams.feed_pending(stream, {"graph_id": "g1"}, delivery_count=1)
    poison = streams.feed_pending(stream, {"graph_id": "g2"}, delivery_count=6)

    reclaimed = asyncio.run(
        reclaim_pending_messages(
            streams, stream, GROUP_TIER1, "worker-1",
            min_idle_ms=1000, max_deliveries=5,
        )
    )
    ids = [m.id for m in reclaimed]
    assert ids == [fresh]  # poison (delivery 6 > 5) is NOT reclaimed
    assert reclaimed[0].data == {"graph_id": "g1"}
    assert poison in streams.pending_data[stream]  # left pending for the reaper


def test_orphaned_pending_tier1_message_is_reclaimed_and_analysed():
    """The C1 kill-test: a graph whose ad.graphs.completed message was delivered
    once and never acked (worker killed mid-tier1) gets reclaimed on the next
    loop pass and analysed — with NO fresh XADD."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orchestrator"), make_run(2, "worker", end_time=2.0)],
            [(1, 2)],
        )
    )
    streams = FakeStreams()
    processor = Tier1Processor(
        repo, FakeObjectStore(), streams, FakeJudge(), make_settings()
    )

    # Seed ONLY an orphaned pending entry (no new '>' message): read() returns
    # nothing, so the graph can only be analysed if reclaim picks it up.
    orphan = streams.feed_pending(
        STREAM_GRAPHS_COMPLETED, {"graph_id": str(uid(1))}, delivery_count=1
    )

    asyncio.run(
        run_tier1(streams, processor, make_settings(), stop=_StopAfter(1))
    )

    # The graph was analysed: a tier1 verdict exists — without any XADD to the
    # source stream (the message came off the pending list, not a fresh publish).
    assert uid(1) in repo.tier1
    assert STREAM_GRAPHS_COMPLETED not in streams.published
    # The reclaimed orphan was claimed back to this consumer and then acked.
    assert orphan in streams.claimed
    assert streams.acked.get(STREAM_GRAPHS_COMPLETED) == [orphan]
