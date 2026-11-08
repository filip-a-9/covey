#!/usr/bin/env bash
# usage: record.sh <dir> [--dry]
# The spend cap: every take (a recording or a --dry rehearsal) starts real Claude Code
# sessions that cost money, so at most 3 are allowed. Delete <dir>/.takes to reset.
set -euo pipefail

# DEMO_TOOLS_BIN: directory with vhs, ttyd and ffmpeg, put first on PATH (optional).
[ -z "${DEMO_TOOLS_BIN:-}" ] || export PATH="$DEMO_TOOLS_BIN:$PATH"

DRY=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--dry" ]; then DRY=1; else ARGS+=("$a"); fi
done
[ ${#ARGS[@]} -eq 1 ] || { echo "usage: record.sh <dir> [--dry]" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "${ARGS[0]}" && pwd)"
MAX_TAKES=3

[ -f "$DIR/env.sh" ] || { echo "Run setup.sh $DIR first." >&2; exit 1; }
takes=0
[ -f "$DIR/.takes" ] && takes="$(cat "$DIR/.takes")"
if [ "$takes" -ge "$MAX_TAKES" ]; then
  echo "Spend cap reached: $takes of $MAX_TAKES takes used (see $DIR/takes.log)." >&2
  echo "Each take runs real Claude Code sessions. To allow more, delete $DIR/.takes." >&2
  exit 1
fi
# Fresh state per take: an `always` grant or last take's requests would change what is on screen.
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env "$DIR"
# Close the last take's panes first: their `covey top` long-poll keeps the old coveyd draining past
# `covey down`'s 10 s, and its late exit would delete the new daemon's socket.
tmux -L covey-demo kill-server 2>/dev/null || true
"$COVEY_BIN" down >/dev/null 2>&1 || true
rm -f "$COVEY_HOME"/covey.db "$COVEY_HOME"/covey.db-wal "$COVEY_HOME"/covey.db-shm
# Rebuild the throwaway repos too: a fix made in a previous take would change the story.
rm -rf "$DIR/api" "$DIR/web"
bash "$HERE/setup.sh" "$DIR" >/dev/null

# Count the take only now: setup starts no claude and costs nothing, so a setup failure
# must not use one up. The cap check above still runs first.
echo $((takes + 1)) > "$DIR/.takes"
mode=record
[ "$DRY" -eq 1 ] && mode=dry
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') take $((takes + 1))/$MAX_TAKES $mode" >> "$DIR/takes.log"

if [ "$DRY" -eq 1 ]; then
  bash "$HERE/layout.sh" "$DIR"
  if [ -t 0 ] && [ -t 1 ]; then exec tmux -L covey-demo attach -t covey-demo; fi
  echo "No TTY: layout started detached. Attach with: tmux -L covey-demo attach -t covey-demo"
  exit 0
fi
cd "$HERE"
DEMO_DIR="$DIR" exec vhs demo.tape
