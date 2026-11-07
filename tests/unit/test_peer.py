"""Ancestry check on a fake process table: pid -> (ppid, argv0)."""

from covey.daemon.peer import ancestors, find_agent, session_root

# 100 a launcher shell, 200 claude, 210 its bash, 220 the caller; 300 an unrelated sibling
# shell under 100; 400 an SDK agent with no claude above it.
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


def start(pid):
    return STARTS.get(pid)


def test_ancestors_walks_to_the_top():
    assert [pid for pid, _ in ancestors(220, TABLE.get)] == [220, 210, 200, 100]


def test_session_root_is_the_nearest_claude():
    assert session_root(220, TABLE.get, start) == (200, "t200")


def test_session_root_falls_back_to_the_peer_without_claude():
    assert session_root(410, TABLE.get, start) == (410, "t410")


def test_session_root_skips_the_daemons_own_ancestors():
    # coveyd sits under the same claude: that claude is not its client's session.
    assert session_root(220, TABLE.get, start, skip={200, 100}) == (220, "t220")


def test_session_root_is_none_for_the_daemon_itself():
    assert session_root(220, TABLE.get, start, skip={220, 210, 200, 100}) is None


def test_descendant_of_a_session_is_found():
    roots = {"alice@n": (200, "t200")}
    assert find_agent(220, roots, TABLE.get, start) == "alice@n"


def test_sibling_is_not_found():
    roots = {"alice@n": (200, "t200")}
    assert find_agent(310, roots, TABLE.get, start) is None


def test_recycled_pid_is_not_found():
    roots = {"alice@n": (200, "an-older-process")}
    assert find_agent(220, roots, TABLE.get, start) is None


def test_the_daemons_own_ancestors_never_match():
    roots = {"alice@n": (200, "t200")}
    assert find_agent(220, roots, TABLE.get, start, skip={200, 100}) is None


def test_worker_process_itself_matches():
    roots = {"w@n": (400, "t400")}
    assert find_agent(410, roots, TABLE.get, start) == "w@n"
