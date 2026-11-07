"""The daemon's database from many threads: FastAPI runs sync handlers in a threadpool."""

import contextlib
import sqlite3
import threading

import pytest

from covey.daemon import registry, store
from covey.daemon.app import Daemon


@pytest.fixture
def daemon(cfg):
    d = Daemon(cfg)
    yield d
    d.close()


def run_threads(*targets):
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)


def test_parallel_reads_from_threads_do_not_error(daemon):
    """Breaks if the threads share one connection (Python 3.13: InterfaceError)."""
    errors: list[Exception] = []

    def read():
        try:
            for _ in range(300):
                registry.agent_by_token_hash(daemon.conn, "0" * 64)
        except sqlite3.Error as exc:
            errors.append(exc)

    run_threads(*[read] * 8)
    assert errors == []


def test_another_threads_write_survives_a_rollback(daemon):
    """Breaks if thread B's transaction joins thread A's, so A's rollback loses B's write."""
    a_open, b_done = threading.Event(), threading.Event()

    def a():
        with contextlib.suppress(RuntimeError), store.transaction(daemon.conn):
            store.audit(daemon.conn, "human@x", "test.a")
            a_open.set()
            b_done.wait(timeout=0.5)  # B, in its own transaction, waits for A to end
            raise RuntimeError("roll back A")

    def b():
        a_open.wait(timeout=5)
        with store.transaction(daemon.conn):
            store.audit(daemon.conn, "human@x", "test.b")
        b_done.set()

    run_threads(a, b)
    events = [
        row[0]
        for row in daemon.conn.execute(
            "SELECT event FROM audit WHERE event LIKE 'test.%'"
        )
    ]
    assert events == ["test.b"]
