# Reply wake through Covey, Claude Code 2.1.319 (2026-11-07)

The hero demo (`examples/two-repos`, rehearsal take 1), driven by hand through `tmux -L covey-demo`.
Two interactive sessions, `api` and `web` (model `sonnet`), in throwaway repos with their own
`COVEY_HOME`, plus `covey top`. Paths are redacted (`<S>` is the scratchpad). Times below are UTC
from `covey-log.json`; the panes show local time (UTC+1).

- 14:36:52 `web` (restarted once, see below) registered. 14:37:06 the SPEC step-1 prompt was typed.
- Request 1 (`explain`, hero flow): created 14:37:14 (8 s after the prompt; `web` went straight to
  `covey_find` + `covey_ask`), `request.wake_posted` in the same ms, delivered 4.3 s later, replied
  13.4 s after that. The idle `api` answered from its git log (commit 43618d7, `currency` required).
- Request 2 (`run_tests`, gated): created 14:38:40 after `web` had edited its client and run its own
  tests. `approval.requested` in the same ms. Human pressed `o` in `covey top` at 14:39:15
  (`grant.created`, `approval.answered`). `request.wake_posted` 1 ms later, delivered 1.75 s later.
- `api` then asked for a permission for an ad-hoc `python3 -c` check (the demo allow-list had no
  `python3:*`); it waited for a keypress until 14:39:50, so `request.replied` at 14:39:53 is not a
  typical latency. `grant.consumed` is in the same ms as the reply.
- Reply wake: `web` was idle after its turn ended (request 2 pending). When the reply landed, `web`
  woke on its own and wrote a follow-up inside Claude Code's peer frame (`web-pane.txt`: the frame, then
  "The backend ran its suite and all 4 tests pass", ~5 s turn). Nobody typed in `web`.
  There is no `request.wake_posted` event for that wake in the audit log (events 22-23 are the last).
- The `once` grant is consumed by the reply, so the `g` view of `covey top` shows "Nothing here yet"
  afterwards (`top-pane.txt` is the activity view). An `always` grant would stay listed.
- Before the run: the first `web` session ran `git ls-files && cat $(...)` and asked for a Bash
  permission (denied, which ended its turn). It was `/exit`ed, the demo allow-list and `web`'s
  CLAUDE.md were changed and `web` was recreated. That is why the log has two `web` registrations.
