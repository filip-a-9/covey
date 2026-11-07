"""Agent-written text must not reach the human's terminal as control sequences (CLI and TUI)."""

import threading
import time

import httpx
import pytest

from tests.integration.test_cli_human import (  # noqa: F401  (fixtures)
    api,
    auth,
    cli,
    cli_json,
    env,
    register,
    tcp,
    web,
)

HOSTILE = (
    "benign\r01JZZ  web -> api  explain  queued  x\r\x1b]0;pwned\x07\nINJECT\x1b[2J"
)


def assert_printable(text):
    for bad in ("\x1b", "\r", "\x07", "\x9b"):
        assert bad not in text
    assert not any(line.startswith("INJECT") for line in text.splitlines())


def hostile_gated(tcp, web, task=HOSTILE):  # noqa: F811
    r = tcp.post(
        "/v1/requests",
        json={"to": "api", "capability": "run_tests", "task": task},
        headers=web,
    )
    assert r.status_code == 201, r.text
    return r.json()["request"]["request_id"]


def test_approvals_text_has_no_terminal_controls_and_one_row_per_request(
    env,  # noqa: F811
    tcp,  # noqa: F811
    web,  # noqa: F811
    api,  # noqa: F811
):
    rid = hostile_gated(tcp, web)
    text = cli("approvals").output
    assert_printable(text)
    assert len([line for line in text.splitlines() if rid in line]) == 1
    assert cli_json("approvals")["requests"][0]["task"] == HOSTILE  # --json untouched


def test_inbox_and_log_text_have_no_terminal_controls(env, tcp, web, api):  # noqa: F811
    cli("ask", "api", HOSTILE)
    assert_printable(cli("inbox", "api").output)
    assert_printable(cli("log").output)


def test_ask_wait_result_is_printable(env, tcp, web, api):  # noqa: F811
    def replier():
        for _ in range(100):
            reqs = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
            if reqs:
                tcp.post(
                    f"/v1/requests/{reqs[0]['request_id']}/reply",
                    json={"result": HOSTILE, "status": "done"},
                    headers=api,
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=replier)
    t.start()
    text = cli("ask", "api", "explain", "--wait").output
    t.join()
    assert_printable(text)


def test_approve_says_what_was_approved(env, tcp, web, api):  # noqa: F811
    rid = hostile_gated(tcp, web, "x" * 300)
    text = cli("approve", rid, "--once").output
    for part in ("once", "web", "api", "run_tests"):
        assert part in text
    assert "x" * 80 in text and "x" * 81 not in text  # the first 80 characters


def test_approve_echo_of_a_hostile_task_is_printable(env, tcp, web, api):  # noqa: F811
    rid = hostile_gated(tcp, web)
    assert_printable(cli("approve", rid).output)


# TUI

pytest.importorskip("textual")

from textual.widgets import OptionList

from covey.tui.app import CoveyTop


async def test_a_hostile_task_makes_one_clean_feed_row(daemon):
    task = "a\nFAKE row\r\x1b]0;pwned\x07\x1b[2J"
    with httpx.Client(base_url=daemon.base_url) as c:
        web_h = register(c, daemon, "web", [("explain", "open")])
        register(c, daemon, "api", [("run_tests", "gated")])
        c.post(
            "/v1/requests",
            json={"to": "api", "capability": "run_tests", "task": task},
            headers=web_h,
        )
        app = CoveyTop(daemon.cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            feed = app.query_one("#feed", OptionList)
            prompts = [o.prompt.plain for o in feed.options]
            card = app.query_one("#approvals").render().plain
    assert len(prompts) == 1
    assert "\n" not in prompts[0]
    for text in (*prompts, card):
        for bad in ("\x1b", "\r", "\x07"):
            assert bad not in text
    assert not any(line.startswith("FAKE") for line in card.splitlines())


async def test_the_approve_key_acts_on_the_request_shown_in_the_card(daemon):
    with httpx.Client(base_url=daemon.base_url) as c:
        web_h = register(c, daemon, "web", [("explain", "open")])
        register(c, daemon, "api", [("run_tests", "gated")])
        ids = []
        for task in ("first", "second"):
            sent = c.post(
                "/v1/requests",
                json={"to": "api", "capability": "run_tests", "task": task},
                headers=web_h,
            )
            ids.append(sent.json()["request"]["request_id"])

        app = CoveyTop(daemon.cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            assert "first" in app.query_one("#approvals").render().plain
            # A refresh reorders the queue between drawing the card and the keypress.
            app.pending = list(reversed(app.pending))
            await pilot.press("d")
            for _ in range(20):
                await pilot.pause(0.2)
                status = c.get(f"/v1/requests/{ids[0]}", headers=web_h)
                if status.json()["request"]["status"] == "refused":
                    break
        final = {
            i: c.get(f"/v1/requests/{i}", headers=web_h).json()["request"]["status"]
            for i in ids
        }
    assert final[ids[0]] == "refused"
    assert final[ids[1]] == "awaiting_grant"
