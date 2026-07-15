"""Pub/sub fan-out + Last-Event-ID resumability."""

from __future__ import annotations

import asyncio

import pytest

from kestrel_feature_observability.fleet.backplane import (
    InProcessPubSub,
    PostgresPubSub,
    create_pubsub,
)

pytestmark = pytest.mark.asyncio


async def _next(sub, timeout=2.0):
    return await asyncio.wait_for(sub.__aiter__().__anext__(), timeout)


async def test_publish_fans_out_to_subscribers():
    ps = InProcessPubSub()
    await ps.start()
    sub = ps.subscribe()
    sid = await ps.publish({"agent_name": "a"})
    stream_id, event = await _next(sub)
    assert stream_id == sid
    assert event["agent_name"] == "a"
    await sub.aclose()
    await ps.stop()


async def test_resume_from_last_event_id_replays_backlog():
    ps = InProcessPubSub()
    await ps.start()
    first = await ps.publish({"n": 1})
    await ps.publish({"n": 2})
    await ps.publish({"n": 3})
    # Reconnect having seen `first`; expect only 2 and 3 replayed.
    sub = ps.subscribe(last_event_id=first)
    a = await _next(sub)
    b = await _next(sub)
    assert [a[1]["n"], b[1]["n"]] == [2, 3]
    await sub.aclose()
    await ps.stop()


async def test_stop_closes_subscribers():
    ps = InProcessPubSub()
    await ps.start()
    sub = ps.subscribe()
    await ps.stop()
    with pytest.raises(StopAsyncIteration):
        await sub.__aiter__().__anext__()


async def test_create_pubsub_variants():
    assert isinstance(create_pubsub("memory"), InProcessPubSub)
    assert isinstance(create_pubsub("postgres", dsn="postgres://x"), PostgresPubSub)
    with pytest.raises(ValueError):
        create_pubsub("postgres")
    with pytest.raises(ValueError):
        create_pubsub("bogus")
