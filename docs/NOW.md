# Now

**Current slice:** MCP shim, done. Next: hooks (SessionStart, Stop, PostToolUse, SessionEnd), then the spawner.

**Done:** schemas; `coveyd` serving every `api.yaml` operation except `/v1/events`; `covey up/down/status/log`;
`covey mcp`, the stdio shim with the nine tools and the `covey://peers` and `covey://inbox` resources.
It registers on start, heartbeats every 20 s, rejoins after a 401, starts `coveyd` if needed, and
writes the 0600 token file hooks will read. Hook payloads from a real Claude Code 2.1.319 session are
in `tests/fixtures/hooks/`, and hook auth is decided (SPEC.md, "How hooks authenticate").

**Blocked:** nothing.

**Before the hooks slice ships:** the contract change that lets `SessionStart` report the current
`session_id` with the agent token (own schema commit); check `--resume` and macOS against the
hook-auth decision.

**Open decisions:** how a `once` grant behaves while its first request is still open, and whether
`GET /v1/agents` without `need` applies `limit` (both in `.claude/specs/review-followups-core-daemon.md`).

**Learned this week:**
- Ranking by token overlap wins 18 of 20 realistic queries over eight cards; the two misses need
  stemming ("remind"/"reminders") or synonyms ("payments"/"billing").
- uvicorn chmods a unix socket it binds itself to 0666, so `coveyd` binds both sockets itself.
- Ruff 0.17's defaults include SIM and B rules; its SIM118 fix is wrong for `sqlite3.Row`.
- Claude Code 2.1.319: the MCP server gets `CLAUDE_CODE_SESSION_ID` in its env, but `/clear` issues a
  new session id without restarting it; the `claude` pid is stable and hooks get it as `CLAUDE_PID`.
  PostToolUse `additionalContext` reaches the model, and fires after built-in tools too.
- The `mcp` SDK (2.3.0) costs about 0.55 s to import, because it loads its client, server, starlette and
  uvicorn; `covey mcp` answers `initialize` in about 0.75 s. Registration runs in the background so a
  daemon start does not delay the handshake.
- A real two-session run (`a` asks `b` over MCP, `b` answers from its inbox, `a`'s `covey_wait` returns)
  works on Claude Code 2.1.319. Claude Code also loads a parent directory's `.mcp.json`.
