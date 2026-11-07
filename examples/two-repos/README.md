# two-repos: the hero demo, scripted

Two Claude Code sessions in two throwaway repos. In `web`, the user reports a 422 from
`/orders`. `web` finds the `api` agent through Covey, asks it what changed, and `api`'s idle
session answers from its git log (yesterday's commit made `currency` required). Then `web`
asks `api` to run its tests, a gated capability, so `covey top` shows one approval. The human
presses `o`, the tests run, `web` fixes its client. The recording ends on `covey top`.

Everything runs in `<dir>/.covey` on port 7788, never in your real `~/.covey`.

## Prerequisites

- `covey` on PATH (`uv tool install` it) or `COVEY_BIN=/absolute/path/to/covey`
- Claude Code (`claude`), logged in. Every take starts two real sessions and costs money.
- `tmux`, `python3`, `git`, `curl`
- `vhs`, `ttyd`, `ffmpeg` (recording only); if they are not on PATH, set `DEMO_TOOLS_BIN=/dir/with/them`
  and `record.sh` puts that directory first on PATH

## Use

```
./setup.sh /tmp/covey-demo           # repos, hooks, MCP, permissions, cards; safe to repeat
./record.sh /tmp/covey-demo --dry    # rehearsal: opens the tmux layout, you drive it
./record.sh /tmp/covey-demo          # records hero.gif with vhs
```

Use the same `<dir>` every time: Claude Code remembers folder trust per path. In the first
rehearsal, accept the trust dialogs in the `api` and `web` panes, then run `claude` in each
(`$DEMO_CLAUDE`), type the prompt, and press `o` in the `covey top` pane when the approval
appears. Panes: `Alt+a` api, `Alt+w` web, `Alt+t` `covey top`.

If vhs would download its own browser into `~/.cache/rod` and you do not want that, run `record.sh` with
`HOME=<other dir>` (with the browser seeded in `<other dir>/.cache/rod/browser/`) and
`DEMO_REAL_HOME=$HOME`; the tape gives the recorded shell the real `HOME` back.

To re-record after a UI change, run `./record.sh <dir>` again. Each take wipes the daemon
database (so an old grant or request can't change what is on screen), rebuilds the two repos and runs `setup.sh`.
Set `CLAUDE_CMD` to change how claude is launched, for example `CLAUDE_CMD="claude --model sonnet"`.

## Spend cap

`record.sh` allows 3 takes per `<dir>`, rehearsals included (`<dir>/.takes`, log in
`<dir>/takes.log`). To allow more, delete `<dir>/.takes`.

## Notes

- `setup.sh` sets the cards with `covey agent add` and `covey describe`. A session that registers
  later keeps its card, so nothing re-applies it.
- Both demo repos carry a short `CLAUDE.md`, as a real team's repo would. It points `web` at
  Covey and at `api`'s tests (capability `run_tests`); you can see the text in `setup.sh`.
- `demo.tape` has not been run against real sessions. Lines marked `[UNVERIFIED]` need a rehearsal.
