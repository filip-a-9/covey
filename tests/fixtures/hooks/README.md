# Recorded Claude Code hook payloads

Real payloads from interactive sessions, used to check hook and shim behaviour against
what Claude Code actually sends instead of what we remember.

## claude-code-2.1.319 (Linux, WSL2, recorded 2026-11-06)

Recorded in a scratch project whose `.claude/settings.json` ran one dump script for
`SessionStart`, `UserPromptSubmit`, `PostToolUse` (matcher `*`), `Stop` and `SessionEnd`,
plus a stdio MCP server that dumped its environment and every message it received.
Each file is one invocation, numbered in the order it happened.

- `fresh/`: start a session, prompt "Ping the lab with lab_ping", then ask whether any
  hook context mentioned a canary word, then `/exit`. The PostToolUse hook returned
  `additionalContext` and the model quoted it back.
- `clear/`: start, ping, `/clear`, ping, `/exit`.

Fields: `stdin` is the hook's JSON input; `mcp_messages` is what the MCP server received;
`ppid_chain` starts at the dumping process's parent and stops at `claude`.

What they show:

- `session_id` on hook stdin equals `CLAUDE_CODE_SESSION_ID` in both the hook and the MCP
  server environment. It is not in the MCP `initialize` params.
- The MCP server's parent is the `claude` process; hooks run under `/bin/sh` whose parent
  is the same process, and get its pid as `CLAUDE_PID`.
- `/clear` sends `SessionEnd` (`reason: clear`) then `SessionStart` (`source: clear`) with a
  new `session_id`. The MCP server is not restarted and keeps the old id in its environment.
- At startup `SessionStart` runs before the MCP server is launched.
- PostToolUse fires for built-in tools (`ToolSearch`) as well as MCP tools.

Sanitized: the environment keeps only the `CLAUDE*` session variables listed in each file
(`CLAUDE_CODE_MESSAGING_TOKEN` and everything else removed), the home directory is
`/home/USER`, the uid is `UID`, and process chains are cut at `claude`.

## claude-code-2.1.319 `covey-init/` (Linux, WSL2, recorded 2026-11-07)

The real `covey hook` commands, as `covey init` installs them, in two scratch repos `a` and `b`
with an isolated `COVEY_HOME`. A wrapper recorded each hook's stdin, stdout, exit code and
wall time (`duration_ms`, includes the Python start). Files are numbered in the order they ran.

Run: `a` asks `b` (`explain`, approved once) while `b` sits idle at its prompt; `b` is given a
one-word turn; the human asks `b` while `b` is mid-turn; `b` runs `/clear`, then `/exit`; `a` runs `/exit`.

What they show:

- An idle session is not woken. Nothing ran in `b` until its next turn ended; then `Stop` blocked
  (05) with the request inside the agent frame, and the follow-up `Stop` carried
  `stop_hook_active: true` and printed nothing (11). Claude Code labels the block "Stop hook error".
- A request that arrives mid-turn is noted by the next `PostToolUse` (15, after `Read`), and the
  model called `covey_inbox` before its turn ended.
- `/clear`: `SessionEnd` (`reason: clear`) then `SessionStart` (`source: clear`); the daemon took the
  new `session_id`.
- `/exit`: `SessionEnd` (`reason: prompt_input_exit`) ran but changed nothing (21, 22): the shim had
  already removed its session token file, so the hook had no token. The agent stayed `live` until
  the 60 s liveness timeout and then went `gone` with its `session_id` still set.
- Every hook costs 340–490 ms of wall time, PostToolUse included, on every tool call.

Sanitized: home is `/home/USER`, the uid is `UID`, repo paths are `/lab/a` and `/lab/b`.

## claude-code-2.1.319 `stop-active/` (Linux, WSL2, recorded 2026-11-07)

The real `covey hook` commands in one scratch repo `b` (Sonnet, `COVEY_IDLE_WAKE=off`, so only the hooks
deliver), with an isolated `COVEY_HOME`. Agent `a` had no session and asked over HTTP. `a` queued THREE while
`b` was idle, and `b` was given a one-word turn. The recording wrapper queued FOUR just after the PostToolUse
for `b`'s first `covey_reply`, which landed it after the model's last tool call in the turn the hook extended.

What they show:

- A block from a Stop that carries `stop_hook_active: true` is honoured. Stop 01 blocked with THREE. The model
  read its inbox and replied, then Stop 04 (`stop_hook_active: true`) blocked with FOUR, and the model handled it.
  Stop 07 (`stop_hook_active: true`) found nothing new and the turn ended. There was no loop.
- A stop-claimed request stays `queued` until the model calls `covey_inbox`. `request.delivered` was audited at
  the inbox read, a few seconds after each block.
- In an earlier take, the second request arrived before the model read its inbox. It came back from that read
  and was answered in the same turn, so no second block was needed.

Sanitized: home is `/home/USER`, the uid is `UID`, the repo path is `/lab/b`.

## claude-code-2.1.319 `resume/` (Linux, WSL2, recorded 2026-11-07)

The real `covey hook` commands in a scratch repo `a` (Sonnet, isolated `COVEY_HOME`), each wrapped by a script
that recorded stdin, stdout, exit code and wall time. Run: start, one `covey_whoami` turn, `covey down && covey up`,
another `covey_whoami` turn, `/exit`, then `claude --resume <id>`. The human then asked `a` while the resumed session sat idle.

What they show:

- `claude --resume <id>` sends `SessionStart` with `source: resume` and the SAME `session_id` as the first run (08).
  The same hook output as a fresh start comes back.
- Covey re-attached the same slug `a` and the same agent id. A request queued after the resume was woken by the idle-wake
  post and came back through `covey_inbox` (`request.wake_posted`, `request.delivered`, then the reply) with no new turn from the user.
- `/exit` marked the agent `gone` straight away (`agent.session_ended`, `reason: shim_exit`). So a request to `a` between
  the `/exit` and the resume is refused (`target_unreachable`, HTTP 422) instead of queued. This differs from the
  `covey-init/` finding above that the agent stayed `live` until the 60 s timeout.
- The prompts these were driven by are not recorded: no `UserPromptSubmit` hook is installed by `covey init`.

Restart check (shim-reattach.md), same run: with the session open, `covey down && covey up` on the same home and port,
then `covey_whoami` from the session returned `a` with no error. The shim re-authenticates against the new daemon on its own.

Sanitized: home is `/home/USER`, the uid is `UID`, the repo is `/lab/a`, session ids are `SESSION-n`, prompt ids `PROMPT-n`.

## claude-code-2.1.319 `turn-cap/` (Linux, WSL2, recorded 2026-11-07)

A wakeable agent `w` (`covey agent add w --wakeable --allowed-tools Read --max-turns 2 --max-budget-usd 0.05`) was asked
to read three files one call at a time. The daemon spawned a headless worker, so nothing was recorded from inside it:
`worker-run.json` is the request and its audit events. `raw-budget-first.json` is the `claude -p --output-format json`
result of the same task run by hand (no MCP, same caps), which is the only raw result recorded.

What they show:

- `--max-turns 2` is enforced. The worker ended with subtype `error_max_turns`, which Covey maps to `worker_error_max_turns`,
  after 5 s and about $0.027. `num_turns` was 3: the cap stops the run once it is exceeded, it does not stop at 2.
- The by-hand run ended on `error_max_budget_usd` with `total_cost_usd` 0.096 against a cap of 0.05. The budget is checked
  after a turn, so one expensive turn (a cold cache write) overshoots it. `is_error` is true and exit code 1 in both.

Sanitized: session ids are `SESSION-n`, the repo is `/lab/w`.

## claude-code-2.1.319 `crashed-session/` (Linux, WSL2, recorded 2026-11-07)

Scratch repo `a` in manual permission mode (`--permission-mode default`). The human asked `a` for `run_tests` (gated) and
approved it with `covey approve --session`. The session replied. Then `kill -9` on the `claude` process, and a new
`claude` in the same repo. `covey-events.json` is `covey log --json` at the end.

What they show:

- No `SessionEnd` hook ran after `kill -9` (09 is the last file of the first session). The shim noticed anyway and the
  daemon audited `agent.session_ended` (`reason: shim_exit`, to `gone`) within seconds.
- The new session registered with `takeover: true` and a different `session_id` (10).
- The `session` grant stays listed in `covey grants`, but a new `run_tests` request to `a` came back `awaiting_grant`
  with an `approval.requested` event: the grant is not inherited.
- Along the way (D): a `covey_inbox` permission prompt left open for about 85 s. The log showed `request.wake_posted` and nothing
  else until the prompt was accepted, then `request.delivered` at the inbox read. No `request.wake_released` and no Stop
  delivery.

Sanitized: as above.
