# Now

**Current slice:** core daemon + HTTP API — done, awaiting commit. Next: MCP shim, hooks, spawner.

**Done:** schemas; `coveyd` (registry, router, grants, store helpers) serving every `api.yaml`
operation except `/v1/events` on the unix socket and `127.0.0.1:$COVEY_PORT`; `covey up/down/status/log`.
The DoD demo (curl registers two agents, asks, long-polls, replies; `covey log` shows the thread)
works by hand. Router 100%, registry 99%, grants 97% covered; an API contract test validates every
response against `api.yaml`.

**Blocked:** how hooks authenticate (SPEC.md, "Decisions still yours"). It blocks the hooks slice.

**Open decisions from this slice's review:** how a `once` grant behaves while its first request is
still open, and whether `wait` should return on `needs_input` (both in `.claude/specs/`).

**Learned this week:**
- Ranking by token overlap wins 18 of 20 realistic queries over eight cards; the two misses need
  stemming ("remind"/"reminders") or synonyms ("payments"/"billing").
- uvicorn chmods a unix socket it binds itself to 0666, so `coveyd` binds both sockets itself.
- Ruff 0.17's defaults include SIM and B rules; its SIM118 fix is wrong for `sqlite3.Row`.
- Hook semantics are still untested on Claude Code 2.1.319. Nothing about them is assumed until a
  real session has been recorded into `tests/fixtures/hooks/`.
