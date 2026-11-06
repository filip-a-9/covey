"""CLI formatting and the not-running paths. No daemon needed."""

import json

from typer.testing import CliRunner

from covey.cli import app, format_log

T1 = "01J00000000000000000000001"
T2 = "01J00000000000000000000002"


def req(rid, thread, frm, to, status, task, created, result=None):
    return {
        "request_id": rid,
        "thread_id": thread,
        "from_agent": frm,
        "to_agent": to,
        "capability": "review",
        "task": task,
        "status": status,
        "result": result,
        "hops": 0,
        "created_at": created,
        "updated_at": created,
    }


PAYLOAD = {
    "covey": "1",
    "events": [],
    "requests": [
        req(
            "r3",
            T2,
            "c@N1",
            "human@N1",
            "queued",
            "other thread",
            "2026-01-01T09:00:00.000Z",
        ),
        req(
            "r2",
            T1,
            "b@N1",
            "c@N1",
            "done",
            "sub task",
            "2026-01-01T10:00:05.000Z",
            "ok done",
        ),
        req(
            "r1", T1, "a@N1", "b@N1", "working", "main task", "2026-01-01T10:00:00.000Z"
        ),
    ],
}


def test_format_log_groups_by_thread_in_time_order():
    lines = format_log(PAYLOAD).splitlines()
    text = "\n".join(lines)
    assert text.index(T2) < text.index(T1)  # thread started 09:00 comes first
    assert text.index("main task") < text.index("sub task")
    assert text.index(T1) < text.index("main task") < text.index("sub task")
    assert text.index("other thread") < text.index(T1)


def test_format_log_line_shows_time_slugs_capability_status_task():
    out = format_log(PAYLOAD)
    assert "10:00:00  a -> b  review  working  main task" in out
    assert "@" not in out


def test_format_log_indents_result_beneath_its_request():
    lines = format_log(PAYLOAD).splitlines()
    i = next(n for n, line in enumerate(lines) if "sub task" in line)
    assert lines[i + 1].startswith("    ")
    assert lines[i + 1].strip() == "ok done"


def test_format_log_empty():
    assert "No requests" in format_log({"covey": "1", "requests": [], "events": []})


def test_format_log_has_no_ansi_dim():
    assert "\033[2m" not in format_log(PAYLOAD)


def test_status_when_not_running_exits_1_and_says_covey_up(tmp_path):
    env = {"COVEY_HOME": str(tmp_path / "h"), "COVEY_PORT": "7999"}
    res = CliRunner().invoke(app, ["status"], env=env)
    assert res.exit_code == 1
    assert "covey up" in res.output


def test_status_json_when_not_running(tmp_path):
    env = {"COVEY_HOME": str(tmp_path / "h"), "COVEY_PORT": "7999"}
    res = CliRunner().invoke(app, ["status", "--json"], env=env)
    assert res.exit_code == 1
    assert json.loads(res.output)["running"] is False


def test_bad_port_exits_nonzero(tmp_path):
    env = {"COVEY_HOME": str(tmp_path / "h"), "COVEY_PORT": "nope"}
    res = CliRunner().invoke(app, ["status"], env=env)
    assert res.exit_code != 0
    assert "COVEY_PORT" in res.output
