# Idle wake through Covey, Claude Code 2.1.319 (2026-11-07)

Two interactive sessions in tmux, `api` and `web`, in throwaway repos set up with `covey init`. They were
in manual permission mode, with their own `COVEY_HOME`. Only covey MCP calls and Read were approved.
Paths are redacted.

- The SessionStart hook wrote `sessions/claude-<pid>.wake.json` (0600, 103 bytes) for both sessions.
  So the SessionStart environment carries both `CLAUDE_PID` and `CLAUDE_CODE_MESSAGING_SOCKET`. Both
  files were removed on `/exit`.
- Run 1 (`covey-log.json`, request `…FXZD1H6`):
  - `api` had no card yet, so `question` went to `awaiting_grant` and `web`'s turn ended.
  - After `covey approve`, `request.wake_posted` came 1 ms after `approval.answered`. The idle `api`
    woke (`api-pane.txt`: Covey's frame inside Claude Code's peer frame, no task text), read its inbox
    18.6 s after the post, and replied.
  - The Stop hook did not deliver it again.
- Run 2 (request `…F29K10`), the hero flow:
  - `api` had described itself (`question: open`).
  - Created and posted in the same millisecond. Delivered 1.8 s later, replied 8.4 s after the ask.
  - `web` got the answer inside its `covey_wait` turn (`web-pane.txt`).
- `/exit`: both shims ended their sessions (`shim_exit`, agents `gone`).
