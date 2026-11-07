"""Daemon.waiting: how many `?wait=` long-polls are open on each request."""

import asyncio

import pytest

from covey.daemon.app import Daemon


@pytest.fixture
def daemon(cfg):
    d = Daemon(cfg)
    yield d
    d.close()


def test_a_request_nobody_waits_on_has_no_waiters(daemon):
    assert daemon.waiting.get("r1", 0) == 0


def test_waiters_are_counted_per_request_and_the_key_goes_at_zero(daemon):
    with daemon.waiting_on("r1"):
        with daemon.waiting_on("r1"):
            assert daemon.waiting["r1"] == 2
            assert daemon.waiting.get("r2", 0) == 0
        assert daemon.waiting["r1"] == 1
    assert "r1" not in daemon.waiting


def test_an_error_inside_the_block_still_decrements(daemon):
    with pytest.raises(RuntimeError), daemon.waiting_on("r1"):
        raise RuntimeError("boom")
    assert "r1" not in daemon.waiting


async def test_a_cancelled_long_poll_decrements(daemon):
    async def long_poll():
        with daemon.waiting_on("r1"):
            await daemon.wait_for(lambda: None, 30)

    task = asyncio.create_task(long_poll())
    await asyncio.sleep(0.05)
    assert daemon.waiting["r1"] == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "r1" not in daemon.waiting
