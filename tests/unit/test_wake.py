"""wake.post: two JSON lines to a unix socket, shaped like the recorded Claude Code send."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from covey import frames
from covey.daemon import wake

FIXTURE = Path("tests/fixtures/hooks/claude-code-2.1.319/messaging/sent.jsonl")


@pytest.fixture
def sock_dir():
    d = Path(tempfile.mkdtemp(prefix="covey-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def serve(path, lines):
    async def handle(reader, writer):
        lines.extend([json.loads(line) async for line in reader])
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_post_sends_the_recorded_two_line_shape_with_our_text(sock_dir):
    lines: list[dict] = []
    server = await serve(sock_dir / "w.sock", lines)
    async with server:
        await wake.post(str(sock_dir / "w.sock"), "hello")
        await asyncio.sleep(0.1)
    sent = [json.loads(x) for x in FIXTURE.read_text().splitlines()]
    assert [set(m) for m in lines] == [set(m) for m in sent]
    assert lines[0]["type"] == "auth" and lines[0]["token"] != sent[0]["token"]
    assert lines[1] == {"type": "user", "message": {"role": "user", "content": "hello"}}


async def test_post_to_a_missing_socket_raises_oserror(sock_dir):
    with pytest.raises(OSError):
        await wake.post(str(sock_dir / "none.sock"), "x")


def test_wake_message_is_the_frame_without_task_text():
    msg = frames.wake_message("api", "review", "01ABC")
    who = "- From `api` (review), request 01ABC"
    assert msg == f"{frames.FRAME}\n{who}\n{frames.CALL_TO_ACTION}"


async def test_post_times_out_with_oserror_when_the_connection_hangs(
    sock_dir, monkeypatch
):
    async def hang(path):
        await asyncio.sleep(30)

    monkeypatch.setattr(asyncio, "open_unix_connection", hang)
    with pytest.raises(OSError):
        await wake.post(str(sock_dir / "w.sock"), "hello", timeout_s=0.1)
