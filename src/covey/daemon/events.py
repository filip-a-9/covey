"""The SSE body for GET /v1/events: audit rows with id > last_id, as they are written."""

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from covey import API_VERSION

if TYPE_CHECKING:
    from covey.daemon.app import Daemon

KEEPALIVE_S = 15.0


def _rows_after(daemon: "Daemon", last_id: int) -> list:
    return daemon.conn.execute(
        "SELECT * FROM audit WHERE id > ? ORDER BY id", (last_id,)
    ).fetchall()


def current_last_id(daemon: "Daemon") -> int:
    row = daemon.conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()
    return row[0]


async def stream(daemon: "Daemon", last_id: int) -> AsyncIterator[str]:
    """Yield one SSE frame per audit row after `last_id`, waiting on daemon.notify()."""
    yield ": covey events\n\n"
    while True:
        rows = await daemon.wait_for(
            lambda after=last_id: _rows_after(daemon, after), KEEPALIVE_S
        )
        if not rows:
            yield ": keepalive\n\n"
            continue
        for row in rows:
            event = {
                "covey": API_VERSION,
                "id": row["id"],
                "ts": row["ts"],
                "actor": row["actor"],
                "event": row["event"],
                "subject": row["subject"],
                "detail": json.loads(row["detail"]),
            }
            last_id = row["id"]
            yield f"id: {last_id}\nevent: {row['event']}\ndata: {json.dumps(event)}\n\n"
