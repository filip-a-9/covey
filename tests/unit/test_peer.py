"""Ancestry check on a fake process table: pid -> (ppid, argv0)."""

from covey.daemon.peer import (
    ancestors,
    find_agent,
    merge_roots,
    peer_chain,
    session_root,
)

# 100 a launcher shell, 200 claude, 210 its bash, 220 the caller; 300 an unrelated sibling
# shell under 100; 400 an SDK agent with no claude above it (its own session, like a shim
# started by a node-named launcher), 410 its child.
TABLE = {
    100: (1, "bash"),
    200: (100, "/usr/bin/claude"),
    210: (200, "bash"),
    220: (210, "covey"),
    300: (100, "bash"),
    310: (300, "covey"),
    400: (100, "python"),
    410: (400, "covey"),
}
STARTS = {pid: f"t{pid}" for pid in TABLE}
SESSIONS = {100: 1, 200: 1, 210: 1, 220: 1, 300: 1, 310: 1, 400: 400, 410: 400}


def start(pid):
    return STARTS.get(pid)


def session_of(pid):
    return SESSIONS.get(pid)


def test_ancestors_walks_to_the_top():
    assert [pid for pid, _ in ancestors(220, TABLE.get)] == [220, 210, 200, 100]


def test_session_root_is_the_nearest_claude():
    assert session_root(220, TABLE.get, start, session=session_of) == (200, "t200")


def test_without_claude_the_root_is_the_oldest_ancestor_in_the_session():
    # 400 is the top of 410's session; launcher 100 is across the setsid boundary.
    assert session_root(410, TABLE.get, start, session=session_of) == (400, "t400")


def test_session_root_without_session_info_is_the_peer():
    root = session_root(410, TABLE.get, start, session=lambda pid: None)
    assert root == (410, "t410")


def test_session_root_skips_the_daemons_own_ancestors():
    # coveyd sits under the same claude: that claude is not its client's session.
    root = session_root(220, TABLE.get, start, skip={200, 100}, session=session_of)
    assert root == (210, "t210")


def test_session_root_is_none_for_the_daemon_itself():
    skip = {220, 210, 200, 100}
    assert session_root(220, TABLE.get, start, skip=skip, session=session_of) is None


def test_chain_must_start_with_the_peer():
    assert peer_chain(220, TABLE.get) == [220, 210, 200, 100]
    assert peer_chain(999, TABLE.get) is None  # exited: no /proc entry


def test_descendant_of_a_session_is_found():
    roots = {"alice@n": [(200, "t200")]}
    assert find_agent([220, 210, 200, 100], roots, start) == "alice@n"


def test_sibling_is_not_found():
    roots = {"alice@n": [(200, "t200")]}
    assert find_agent([310, 300, 100], roots, start) is None


def test_recycled_pid_is_not_found():
    roots = {"alice@n": [(200, "an-older-process")]}
    assert find_agent([220, 210, 200, 100], roots, start) is None


def test_the_daemons_own_ancestors_never_match():
    roots = {"alice@n": [(200, "t200")]}
    assert find_agent([220, 210, 200, 100], roots, start, skip={200, 100}) is None


def test_worker_process_itself_matches():
    roots = {"w@n": [(400, "t400")]}
    assert find_agent([410, 400, 100], roots, start) == "w@n"


def test_a_worker_root_does_not_mask_the_session_root():
    roots = {"a@n": [(400, "t400"), (200, "t200")]}
    assert find_agent([220, 210, 200, 100], roots, start) == "a@n"
    assert find_agent([410, 400, 100], roots, start) == "a@n"


def test_merge_roots_keeps_both():
    merged = merge_roots({"a@n": (200, "x")}, {"a@n": (400, "y"), "b@n": (1, "z")})
    assert merged == {"a@n": [(200, "x"), (400, "y")], "b@n": [(1, "z")]}
