"""CLI formatting and the not-running paths. No daemon needed."""

import json
import sys

from typer.testing import CliRunner

from covey.cli import app, format_follow, format_log

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


def ev(eid, event, subject, ts, actor="b@N1", detail=None):
    return {
        "covey": "1",
        "id": eid,
        "ts": ts,
        "actor": actor,
        "event": event,
        "subject": subject,
        "detail": detail or {},
    }


WITH_EVENTS = {
    **PAYLOAD,
    "events": [
        ev(1, "request.created", "r2", "2026-01-01T10:05:00.000Z"),
        ev(2, "agent.liveness", "c@N1", "2026-01-01T10:05:01.000Z", actor="c@N1"),
        ev(
            3,
            "approval.answered",
            "r2",
            "2026-01-01T10:05:02.000Z",
            actor="human@N1",
            detail={"scope": "once", "grant_id": "g1"},
        ),
        ev(4, "request.delivered", "r2", "2026-01-01T10:05:03.000Z", actor="c@N1"),
        ev(5, "request.replied", "r2", "2026-01-01T10:05:09.000Z", actor="c@N1"),
    ],
}


def lines_under(text, needle):
    """The indented lines directly beneath the request line containing `needle`."""
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if needle in line)
    out = []
    for line in lines[i + 1 :]:
        if not line.startswith("    "):
            break
        out.append(line.strip())
    return out


def test_format_log_indents_result_beneath_its_request():
    assert lines_under(format_log(PAYLOAD), "sub task") == ["ok done"]


def test_format_log_shows_request_events_beneath_it_with_result_last():
    under = lines_under(format_log(WITH_EVENTS), "sub task")
    assert under == [
        "10:05:02  approval.answered  human  scope=once",
        "10:05:03  request.delivered  c",
        "ok done",
    ]


def test_format_log_leaves_out_events_not_about_a_shown_request():
    out = format_log(WITH_EVENTS)
    assert "agent.liveness" not in out
    assert "request.created" not in out
    assert "request.replied" not in out


SUB = PAYLOAD["requests"][1]  # r2, thread T1, b -> c, done, "ok done"


def test_format_follow_created_names_thread_agents_and_task():
    e = ev(1, "request.created", "r2", "2026-01-01T10:05:00.000Z")
    assert format_follow(e, SUB) == [
        "10:05:00  000001  b -> c  request.created  sub task"
    ]


def test_format_follow_replied_adds_status_and_indented_result():
    e = ev(5, "request.replied", "r2", "2026-01-01T10:05:09.000Z", actor="c@N1")
    assert format_follow(e, SUB) == [
        "10:05:09  000001  b -> c  request.replied  done",
        "    ok done",
    ]


def test_format_follow_other_events_carry_their_detail():
    e = ev(
        3,
        "approval.answered",
        "r2",
        "2026-01-01T10:05:02.000Z",
        actor="human@N1",
        detail={"scope": "once"},
    )
    assert format_follow(e, SUB) == [
        "10:05:02  000001  b -> c  approval.answered  scope=once"
    ]


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


def test_top_without_the_tui_extra_names_the_uv_tool_reinstall(monkeypatch):
    # The README installs with `uv tool install`; pip cannot reach that environment.
    monkeypatch.setitem(
        sys.modules, "covey.tui.app", None
    )  # import now raises ImportError
    res = CliRunner().invoke(app, ["top"])
    assert res.exit_code == 1
    assert "uv tool install" in res.output
    assert "covey-mesh[tui]" in res.output
    assert "pip install" not in res.output


def test_windows_exits_2_with_a_wsl_hint(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 2
    assert "WSL" in res.output


def test_version_flag_prints_installed_version():
    from importlib.metadata import version

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("covey-mesh")
    assert version("covey-mesh") in result.output


def test_no_args_still_prints_help():
    result = CliRunner().invoke(app, [])
    assert "Usage" in result.output
    assert result.exit_code in (0, 2)
