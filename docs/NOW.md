# Now

**Current slice:** trust, done (`.claude/specs/trust.md`, `.claude/specs/trust-followups.md` items 1–2).
Checked in a real Claude Code session, with coveyd started the way the shim starts it:
- `covey approve` from inside the session is refused and names the agent.
- The same command from outside the session's process tree succeeds.
- `covey doctor` shows the refusal.
- Not yet seen: the toast on screen.

Next: docs + hero GIF.

**Done:**
- Schemas; `coveyd` serving every `api.yaml` operation.
- The `covey mcp` shim with the nine tools (and `--worker` mode).
- The four hooks, and `covey init [--user]`.
- The CLI with `--json`, including `covey agent add`.
- The Python SDK, the `/v1/events` stream and `covey top`.
- The waker (`daemon/waker.py`): claims `queued` requests for wakeable agents (one worker per agent, two
  overall, rolling 24 h budget), runs `claude -p` with a memory-only worker token, falls back to a reply from
  the worker's result, kills hung workers, and marks requests `worker_lost` after a restart.
- Trust slice:
  - The human token is refused from inside an agent's process tree. The daemon reads the peer pid on
    the unix socket and keeps session roots in memory (`daemon/peer.py`), so there is no DDL change. It
    fails closed when the peer pid is unreadable.
  - Desktop notifications (`daemon/notify.py`): a toast through `powershell.exe` on WSL, plyer
    elsewhere, `COVEY_NOTIFY=log` to turn them off. Approvals notify only when no human watcher is
    connected.
  - Workers may have Bash when the human lists it.

**Blocked:** nothing.

**Open decisions:**
- An idle live session is never woken (`.claude/specs/hooks-real-session-findings.md`, finding 2). Claude
  Code documents a cross-session inbox socket that may fix it; a spike plan is in that file, not scheduled.
- Sweep clearing `session_id` (finding 1, option C) was dropped: it detaches a session for good after laptop
  sleep. Revisit together with a way for the shim to re-attach.
- `covey doctor` should check that `coveyd` can find `claude` (`COVEY_CLAUDE`). The daemon's PATH may differ
  from the shell's.
- Stop hook returns early on `stop_hook_active`, so a request arriving during the blocked turn waits for the
  next turn end. Claim anyway?
- stop-claim stamps the request before the hook prints the block; a hook killed in between strands it.
- `covey doctor` should report the active notify backend. coveyd started without Windows PATH interop
  (systemd, some `uv` setups) can't find `powershell.exe` and silently logs only.
- `test_lifecycle.py::test_two_shims_starting_the_daemon_at_once_both_get_it` leaks one coveyd per run. A
  pidfile race is suspected, which would also hit two sessions opening at once
  (`.claude/specs/trust-followups.md` item 3).
- `PUT /v1/agents/{slug}/session` doesn't record a session root. Only register and heartbeat do, so after
  `/clear` the root waits for the next heartbeat (≤20 s).
- `covey init` writes bare `covey hook ...`; under `uv run` in a dev checkout that needs the venv on PATH.
  Add `--command`?

**Learned this week:**
- Real-session check, Claude Code 2.1.319, from inside a session:
  - Its Bash process tree has `claude` as argv0, so the walk finds it.
  - With coveyd started outside, the daemon refuses `covey approve` from inside and accepts it from a
    process outside the tree.
  - The text reaches `powershell.exe` through `WSLENV` unchanged, and `$(...)` isn't run.
  - The toast `Show()` succeeds. No one has seen it on screen yet.
  - No notification while a `/v1/events` stream is open; one after it closes.
- uvicorn 0.54 has no hook for the peer socket. Subclassing its HTTP protocol class and wrapping
  `self.app` in `connection_made` puts the peer pid in every request scope. Setting `conf.http` after
  `load()` has no effect: pass `http=` to a fresh `Config`.
- The shim auto-starts coveyd. Unless it is detached through an intermediate process, the first session
  stays coveyd's parent. `ensure_daemon` now detaches it. Claude Code is not a subreaper: the detached
  coveyd ends up under WSL's init relay.
- A test daemon runs inside pytest, which may itself run under Claude Code. It passes
  `serve(ignore_pids=peer.own_ancestry())`. Production ignores nothing.
- Ranking by token overlap wins 18 of 20 realistic queries over eight cards. The two misses need stemming
  or synonyms.
- uvicorn chmods a unix socket it binds itself to 0666, so `coveyd` binds both sockets itself.
- Ruff 0.17's SIM118 fix is wrong for `sqlite3.Row`.
- Claude Code 2.1.319, sessions:
  - `/clear` issues a new session id without restarting the MCP server; hooks get the stable pid as
    `CLAUDE_PID`.
  - PostToolUse and SessionStart `additionalContext` reach the model.
  - A Stop block re-prompts once; the next Stop carries `stop_hook_active: true`.
  - On `/exit` the shim closes before SessionEnd runs, so the shim now ends its own session.
- Claude Code 2.1.319, `claude -p` (`tests/fixtures/worker/`):
  - `--max-turns` is accepted but hidden from `--help`.
  - `--restricted` still loads `CLAUDE.md`.
  - Read-only Bash runs without being allowed.
  - Denials don't fail the run; `result` is absent on errors.
  - `--max-budget-usd` overshoots by a turn.
  - The default model spends $0.19 in 3 turns; Sonnet answers a real question for $0.017 in about 6–8 s.
- `python -m covey.cli` does not work (no `__main__`); worker `mcp.json` uses
  `python -c "from covey.cli import app; app()"`.
- Every hook costs 340–490 ms of Python start-up.
- Tests can never reach the installed `claude`: fixtures point `claude_bin` and `COVEY_CLAUDE` at a missing
  path. Worker tests use `tests/fixtures/worker/stub_claude.py`; `COVEY_TEST_REAL_CLAUDE=1` checks the
  worker flags against `claude --help`.
