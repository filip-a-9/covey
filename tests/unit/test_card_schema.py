"""The card contract itself (src/covey/schema/card.json), independent of the registry."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (Path(__file__).parents[2] / "src" / "covey" / "schema" / "card.json").read_text()
)
VALIDATOR = Draft202012Validator(SCHEMA)


def card(**kw):
    base = {
        "covey": "1",
        "agent_id": "api@01JC3Z8K2M4N5P6Q7R8S9T0V1W",
        "slug": "api",
        "kind": "claude-code",
        "workdir": "/home/u/code/api",
        "summary": "FastAPI backend.",
        "tags": ["python"],
        "capabilities": [{"name": "explain", "level": "open"}],
        "delivery": "hooks",
        "wakeable": True,
        "liveness": "wakeable",
        "last_seen": "2026-11-06T14:02:11Z",
        "public_key": None,
    }
    return base | kw


def valid(c):
    return not list(VALIDATOR.iter_errors(c))


def test_a_card_without_worker_is_still_valid():
    assert valid(card())


def test_a_full_worker_block_is_valid():
    worker = {
        "allowed_tools": ["Read", "Grep", "Glob"],
        "max_turns": 10,
        "max_budget_usd": 1.0,
        "daily_spawns": 20,
    }
    assert valid(card(worker=worker))


def test_an_empty_worker_block_is_valid():
    assert valid(card(worker={}))


@pytest.mark.parametrize(
    "worker",
    [
        {"surprise": 1},
        {"allowed_tools": "Read"},
        {"allowed_tools": ["Read", "Read"]},
        {"allowed_tools": [""]},
        {"max_turns": 0},
        {"max_turns": 2.5},
        {"max_budget_usd": 0},
        {"max_budget_usd": -1},
        {"daily_spawns": 0},
    ],
)
def test_a_malformed_worker_block_is_invalid(worker):
    errors = list(VALIDATOR.iter_errors(card(worker=worker)))
    assert errors
    assert all(list(e.path)[:1] == ["worker"] for e in errors)
