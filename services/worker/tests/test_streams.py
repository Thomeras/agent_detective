"""Streams: message-envelope decoding and the dead-letter reaper."""

import asyncio

from worker.streams import DLQ_SUFFIX, _decode, reap_dead_letters
from worker.types import PendingEntry, StreamMessage

from conftest import FakeStreams


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
