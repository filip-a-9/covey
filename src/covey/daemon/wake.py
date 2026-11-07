"""Idle wake: post one message to a Claude Code session's messaging socket.

The shape is the recorded send in tests/fixtures/hooks/claude-code-2.1.319/messaging/.
Claude Code does not check the token, so a dummy goes out: Covey never holds the real one.
"""

import asyncio
import json

DUMMY_TOKEN = "covey"


async def post(path: str, text: str, timeout_s: float = 2.0) -> None:
    """Send `text` as a user message. Raises OSError on any failure, a timeout included."""
    lines = (
        {"type": "auth", "token": DUMMY_TOKEN},
        {"type": "user", "message": {"role": "user", "content": text}},
    )
    data = "".join(json.dumps(line) + "\n" for line in lines).encode()

    async def send() -> None:
        _, writer = await asyncio.open_unix_connection(path)
        try:
            writer.write(data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        await asyncio.wait_for(send(), timeout_s)
    except TimeoutError as exc:
        raise OSError(f"timed out posting to {path}") from exc
