# Cross-session messaging, Claude Code 2.1.319 (2026-11-07)

One interactive session in tmux, idle at its prompt, in manual permission mode. A script sent two
JSON lines to `$CLAUDE_CODE_MESSAGING_SOCKET` (see `sent.jsonl`). Token and paths are redacted.

- `session-start-stdin.json`: the SessionStart payload. Its environment carried the socket and token.
- `idle-wake-pane.txt`: the first message woke the idle session, and Claude Code framed it as from another session.
- `edge-cases-pane.txt`, in order:
  - Wrong token: still delivered. Auth isn't checked on the unix socket. The socket is 0600 in a 0700 dir.
  - A peer asks for `touch`: the Bash permission prompt appears for the human. The peer grants nothing.
  - A second message while that prompt is open: shown queued. It runs after the prompt is answered.
  - Plan mode: the message still starts a turn.
  - A message mid-turn: queued, then runs as the next turn.
  - `/clear`: socket path and token unchanged (new `session_id`). The old address still delivers.
- The socket never writes anything back.
