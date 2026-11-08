# two-repos: the hero demo, scripted

Two Claude Code sessions in two throwaway repos. In `web`, the user reports a 422 from
`/orders`. `web` finds the `api` agent through Covey, asks it what changed, and `api`'s idle
session answers from its git log (yesterday's commit made `currency` required). Then `web`
asks `api` to run its tests, a gated capability, so `covey top` shows one approval. The human
presses `a` (always), the tests run, `web` fixes its client. The recording ends on `covey top`.

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
(`$DEMO_CLAUDE`), type the prompt, and press `a` in the `covey top` pane when the approval
appears. Panes: `Alt+a` api, `Alt+w` web, `Alt+t` `covey top`.

If vhs would download its own browser into `~/.cache/rod` and you do not want that, run `record.sh` with
`HOME=<other dir>` (with the browser seeded in `<other dir>/.cache/rod/browser/`) and
`DEMO_REAL_HOME=$HOME`; the tape gives the recorded shell the real `HOME` back.

To re-record after a UI change, run `./record.sh <dir>` again. Each take wipes the daemon
database (so an old grant or request can't change what is on screen), rebuilds the two repos and runs `setup.sh`.
Set `CLAUDE_CMD` to change how claude is launched, for example `CLAUDE_CMD="claude --model sonnet"`.

To keep your own Claude Code setup (`~/.claude` rules, `CLAUDE.md`, hooks) out of the recording, set
`DEMO_HOME=<empty dir>` and use `<dir>=$DEMO_HOME/code`. The `api` and `web` panes then run with that
`HOME`, so the sessions show `~/code/api` and `~/code/web`. That home has no login: run `/login`
in a pane during the first `--dry` rehearsal.

## Spend cap

`record.sh` allows 3 takes per `<dir>`, rehearsals included (`<dir>/.takes`, log in
`<dir>/takes.log`). To allow more, delete `<dir>/.takes`.

## Notes

- `setup.sh` sets the cards with `covey agent add` and `covey describe`. A session that registers
  later keeps its card, so nothing re-applies it.
- Both demo repos carry a short `CLAUDE.md`, as a real team's repo would. It points `web` at
  Covey and at `api`'s tests (capability `run_tests`); you can see the text in `setup.sh`.
- Take 3 ran `demo.tape` against real sessions, so its Alt+letter keys, startup time and
  `Wait+Screen` patterns are verified. Take counting: a take is counted after `setup.sh` succeeds.

## Recording from scratch

Tools go in the repo's gitignored `.claude/demo-tools/bin` (no sudo, no package manager) and are
deleted after the recording. Then `export DEMO_TOOLS_BIN=$PWD/.claude/demo-tools/bin`.
Always check the hash before unpacking.

| Tool | Version | URL | SHA-256 |
|---|---|---|---|
| vhs | v0.12.1 | `https://github.com/charmbracelet/vhs/releases/download/v0.12.1/vhs_0.12.1_Linux_x86_64.tar.gz` | `eb33787a5b1cebf91fe0b18bd231e2d71d36c1ff4babd99ddac1a6ab411efd10` |
| ttyd | 1.7.7 | `https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64` | `8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55` |
| ffmpeg | 6.0.1 static | `https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-6.0.1-amd64-static.tar.xz` | UNVERIFIED: compute with sha256sum after download (the site publishes only an md5, and none for old releases) |
| chrome-headless-shell | 157.0.8103.38 | `https://storage.googleapis.com/chrome-for-testing-public/157.0.8103.38/linux64/chrome-headless-shell-linux64.zip` | UNVERIFIED: compute with sha256sum after download (Google publishes none); URL layout unchecked, confirm on first run |

```
T=.claude/demo-tools; mkdir -p $T/bin $T/dl
curl -L -o $T/dl/vhs.tar.gz <vhs URL>; echo "<sha>  $T/dl/vhs.tar.gz" | sha256sum -c -
tar -xzf $T/dl/vhs.tar.gz -C $T/bin vhs
curl -L -o $T/bin/ttyd <ttyd URL>; echo "<sha>  $T/bin/ttyd" | sha256sum -c -; chmod +x $T/bin/ttyd
tar -xJf <ffmpeg archive> --strip-components=1 -C $T/bin --wildcards '*/ffmpeg'
unzip $T/dl/chrome-headless-shell-linux64.zip -d $T/chromium
```

Chromium: vhs 0.12.1 downloads its own into `~/.cache/rod` and ignores `rod=bin=` and
`XDG_CACHE_HOME`. Give it a scratch HOME whose `.cache/rod` points at the Chromium above:

```
mkdir -p /tmp/covey-rec-home/.cache && ln -s "$PWD/.claude/demo-tools/chromium" /tmp/covey-rec-home/.cache/rod
```

The directory layout rod expects inside (e.g. `browser/<revision>/chrome`) is not confirmed; check
it on the first run. Record with `HOME=/tmp/covey-rec-home DEMO_REAL_HOME=$HOME`.

Short paths: `DEMO_HOME=/tmp/covey-demo-home` and `<dir>=$DEMO_HOME/code`. A long path (a scratchpad
directory) overflows the AF_UNIX socket path limit and the daemon will not start.

Fresh Claude Code home, once:

1. `./setup.sh $DEMO_HOME/code` then `./record.sh $DEMO_HOME/code --dry` (a rehearsal; it counts as a take).
2. `/login` in a pane; paste the OAuth code with `tmux -L covey-demo send-keys -t covey-demo:0.<pane> '<code>' Enter`.
3. Click through the first-run screens, then the folder-trust dialogs for `api` and `web`.
4. Record: `HOME=/tmp/covey-rec-home DEMO_REAL_HOME=$HOME DEMO_HOME=/tmp/covey-demo-home CLAUDE_CMD="claude --model sonnet" ./record.sh $DEMO_HOME/code`

Spend: 3 takes per `<dir>`, rehearsals included; reset by deleting `<dir>/.takes`.

Clean up afterwards: `rm -rf .claude/demo-tools /tmp/covey-rec-home /tmp/covey-demo-home`

Acceptance, all of:

- built from `main`'s tip (`git rev-parse --short HEAD`) and `uv run covey doctor --json` is clean
- the `api` pane shows no `covey_describe` talk and no permission prompt for any tool (watch it; the tape cannot detect one)
- the `web` pane reaches the fix; `covey top` shows exactly one approval, `a` answers it, the feed ends `● done`
- about 100 s or less (`ffprobe hero.gif`), grant to wake a couple of seconds
- file size no larger than take 3 plus 20% (1.9 MB, so about 2.3 MB)
