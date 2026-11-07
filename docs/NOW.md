# Now

**Current slice:** spawner, done (`.claude/specs/spawner.md`). A `wakeable` agent with no session is served
by a headless `claude -p` worker; spawn budget, concurrency and timeout are enforced. Next: the trust slice
(ancestry check, desktop notifications behind `notify_human`, then Bash for workers).

**Done:**
- Schemas; `coveyd` serving every `api.yaml` operation.
- The `covey mcp` shim with the nine tools (and `--worker` mode).
- The four hooks, and `covey init [--user]`.
- The CLI with `--json`, including `covey agent add`.
- The Python SDK, the `/v1/events` stream and `covey top`.
- The waker (`daemon/waker.py`): claims `queued` requests for wakeable agents (one worker per agent, two
  overall, rolling 24 h budget), runs `claude -p` with a memory-only worker token, falls back to a reply from
  the worker's result, kills hung workers, and marks requests `worker_lost` after a restart.

**Blocked:** nothing.

**Open decisions:**
- An idle live session is never woken (`.claude/specs/hooks-real-session-findings.md`, finding 2). Claude
  Code documents a cross-session inbox socket that may fix it; a spike plan is in that file, not scheduled.
- Sweep clearing `session_id` (finding 1, option C) was dropped: it detaches a session for good after laptop
  sleep. Revisit together with a way for the shim to re-attach.
- `api.yaml` names four worker codes. The waker also uses `worker_error`, `worker_no_result` and
  `worker_<subtype>`. Document them in a schema commit?
- `covey agent add` cannot declare capabilities, so every request to a pre-registered agent needs an
  approval until a session describes it. Add `--capabilities`?
- `covey doctor` should check that `coveyd` can find `claude` (`COVEY_CLAUDE`). The daemon's PATH may differ
  from the shell's.
- Stop hook returns early on `stop_hook_active`, so a request arriving during the blocked turn waits for the
  next turn end. Claim anyway?
- stop-claim stamps the request before the hook prints the block; a hook killed in between strands it.
- `covey init` writes bare `covey hook ...`; under `uv run` in a dev checkout that needs the venv on PATH.
  Add `--command`?

**Learned this week:**
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
