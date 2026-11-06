# Now

**Current slice:** hooks, merged to `main` but not yet checked on a real session. Next: the real-session check below,
then the spawner (`.claude/specs/spawner.md`, has open questions).

**Done:** schemas; `coveyd` serving every `api.yaml` operation except `/v1/events`; `covey up/down/status/log`;
`covey mcp`, the stdio shim with the nine tools and two resources; agent session endpoints
(`PUT/DELETE /v1/agents/{slug}/session`), `inbox/stop-claim` and `inbox?peek=true`; `covey hook
session-start|stop|post-tool|session-end`, which always exit 0; `covey init [--user]`, which writes the four hooks and
the MCP entry; `once` grants allow one request in flight (wildcard included); `GET /v1/agents` without `need` lists all.

**Blocked:** nothing.

**Before the hooks slice counts as done:** a real two-session run through `covey init` hooks (Stop wakes `b`, PostToolUse note,
`/clear` swaps the session id, `/exit` ends it); record the Stop block / `stop_hook_active: true` and SessionStart
output payloads into `tests/fixtures/hooks/`; check `--resume` and macOS against the hook-auth decision.

**Open decisions:**
- An ended session is stored as `session_id = ""` so a late shim heartbeat cannot revive it, while sessionless SDK
  agents still revive. Keep, or add a column with a migration?
- Stop hook returns early on `stop_hook_active`, so a request arriving during the blocked turn waits for the next
  turn end. Claim anyway (block-once already prevents loops)?
- stop-claim stamps the request before the hook prints the block; a hook killed in between strands the request.
- `covey init` writes bare `covey hook ...`; under `uv run` in a dev checkout that needs the venv on PATH. Add
  `--command`?
- Spawner open questions are at the end of `.claude/specs/spawner.md`.

**Learned this week:**
- Ranking by token overlap wins 18 of 20 realistic queries over eight cards; the two misses need
  stemming ("remind"/"reminders") or synonyms ("payments"/"billing").
- uvicorn chmods a unix socket it binds itself to 0666, so `coveyd` binds both sockets itself.
- Ruff 0.17's defaults include SIM and B rules; its SIM118 fix is wrong for `sqlite3.Row`.
- Claude Code 2.1.319: the MCP server gets `CLAUDE_CODE_SESSION_ID` in its env, but `/clear` issues a
  new session id without restarting it; the `claude` pid is stable and hooks get it as `CLAUDE_PID`.
  PostToolUse `additionalContext` reaches the model, and fires after built-in tools too.
- Claude Code 2.1.319: SessionStart `hookSpecificOutput.additionalContext` reaches the model; a Stop
  `{"decision":"block","reason":...}` re-prompts once and the next Stop carries `stop_hook_active: true`.
- `claude mcp add -s project` writes `{"type":"stdio","command":...,"args":[...],"env":{}}` into `.mcp.json`.
- The `mcp` SDK (2.3.0) costs about 0.55 s to import; `covey mcp` answers `initialize` in about 0.75 s.
  Each hook is also a Python start: measure PostToolUse latency in the real-session run.
- A real two-session run (`a` asks `b` over MCP, `b` answers from its inbox, `a`'s `covey_wait` returns)
  works on Claude Code 2.1.319. Claude Code also loads a parent directory's `.mcp.json`.
