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
