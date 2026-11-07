#!/usr/bin/env bash
# usage: layout.sh <dir>
# Starts coveyd for <dir>/.covey if needed and builds the tmux session `covey-demo`
# (100x30) on its own tmux server (`tmux -L covey-demo`, no user config), with:
#   pane 0 (top left)   api   repo, env sourced, ready for $CLAUDE_CMD
#   pane 1 (top right)  web   repo, env sourced, ready for $CLAUDE_CMD
#   pane 2 (bottom)     covey top
# Alt+a / Alt+w / Alt+t (api, web, top) jump to panes 0 / 1 / 2 without a prefix key.
# Each repo pane has $DEMO_CLAUDE set to the launch command; the tape types `$DEMO_CLAUDE`.
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: layout.sh <dir>" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "$1" && pwd)"
[ -f "$DIR/env.sh" ] || { echo "Run setup.sh $DIR first." >&2; exit 1; }
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env "$DIR"
CLAUDE_CMD="${CLAUDE_CMD:-claude}"
T=(tmux -L covey-demo -f /dev/null)

"$COVEY_BIN" up >/dev/null
"${T[@]}" kill-session -t covey-demo 2>/dev/null || true

"${T[@]}" new-session -d -s covey-demo -x 100 -y 30 -n demo
"${T[@]}" set-option -t covey-demo status off
# Claude Code prints a hint about this setting in every pane otherwise.
"${T[@]}" set-option -g focus-events on
"${T[@]}" set-option -t covey-demo -g pane-base-index 0
"${T[@]}" set-option -t covey-demo pane-border-status top
"${T[@]}" set-option -t covey-demo pane-border-format ' #{pane_title} '
"${T[@]}" split-window -t covey-demo:demo.0 -v -l 15
"${T[@]}" split-window -t covey-demo:demo.0 -h
# vhs can send Alt+<letter> but not Alt+<digit>.
i=0
for k in a w t; do
  "${T[@]}" bind-key -n "M-$k" select-pane -t "covey-demo:demo.$i"
  i=$((i + 1))
done
"${T[@]}" select-pane -t covey-demo:demo.0 -T api
"${T[@]}" select-pane -t covey-demo:demo.1 -T web
"${T[@]}" select-pane -t covey-demo:demo.2 -T "covey top"

for pane in 0:api 1:web; do
  n="${pane%%:*}"
  repo="${pane##*:}"
  "${T[@]}" send-keys -t "covey-demo:demo.$n" \
    ". '$DIR/env.sh'; export PS1='\$ ' DEMO_CLAUDE='$CLAUDE_CMD'; cd '$DIR/$repo'; clear" Enter
done
"${T[@]}" send-keys -t covey-demo:demo.2 ". '$DIR/env.sh'; clear; exec '$COVEY_BIN' top" Enter

"${T[@]}" select-window -t covey-demo:demo
"${T[@]}" select-pane -t covey-demo:demo.1
echo "tmux session ready: tmux -L covey-demo attach -t covey-demo"
