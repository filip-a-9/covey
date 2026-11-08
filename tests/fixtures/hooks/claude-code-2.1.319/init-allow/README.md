# init-allow: covey tools pre-allowed by `covey init`

Recorded with Claude Code 2.1.319 in tmux, Sonnet, `--permission-mode default --setting-sources project,local
--strict-mcp-config`, one scratch repo per case.

- `settings-allow.json`: `.claude/settings.json` after `covey init` (hooks plus nine `permissions.allow` rules).
  `settings-no-allow.json`: after `covey init --no-allow` (hooks only). The covey path is replaced by `/path/to/covey`.
- `allow-session-pane.txt`: prompt "call covey_find, then covey_ask". Both ran ("Called covey 2 times") with no permission prompt.
- `no-allow-session-pane.txt`: same prompt; Claude Code asks "Do you want to proceed?" for `covey_find` (declined).
- `trust-dialog-with-allow.txt`: the one-time folder trust dialog lists the nine pre-approved tools.

Notes: the port 7777 default was taken by another coveyd, so `COVEY_PORT` was set. The first Enter after pasting a
prompt into the pane was swallowed; a second Enter submitted it.
