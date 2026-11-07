# Hero demo: two Claude Code sessions, one asks the other, the human approves one grant.
# Record with:  ./record.sh <dir>      (sets DEMO_DIR; counts as a take, max 3)
# Nothing in this tape has been run against a real session yet. Lines marked
# [UNVERIFIED] depend on Claude Code's or vhs's real behaviour and need a --dry rehearsal.

Output hero.gif

Set Shell "bash"
Set FontSize 14
# 960x540 at this font is a 100x30 terminal, the size layout.sh builds.
Set Width 960
Set Height 540
Set Padding 10
Set TypingSpeed 40ms
Set Theme "Dracula"
Set PlaybackSpeed 1.5

# Build the layout and attach, hidden. DEMO_DIR comes from record.sh.
Hide
# vhs caches its browser under $HOME/.cache/rod; record.sh may run it with another HOME (see
# README), so give the recorded shell the real one back. DEMO_REAL_HOME is optional.
Type "export HOME=${DEMO_REAL_HOME:-$HOME}"
Enter
Type "export DEMO_DIR=${DEMO_DIR:?run via record.sh}"
Enter
Type "bash layout.sh $DEMO_DIR && tmux -L covey-demo attach -t covey-demo"
Enter
Sleep 3s
# [UNVERIFIED] vhs forwards Alt+<letter>; layout.sh binds Alt+a/w/t to panes 0/1/2 (vhs has no Alt+<digit>).
Alt+a
Type "$DEMO_CLAUDE"
Enter
Alt+w
Type "$DEMO_CLAUDE"
Enter
# [UNVERIFIED] Claude Code's startup time. The repos' directories must already be trusted
# (accept the folder-trust dialogs once in a --dry rehearsal; Claude Code remembers them).
Sleep 20s
# [UNVERIFIED] both agents live and carrying the cards setup.sh set.
Wait+Screen@120s /api\s+claude/
Wait+Screen@60s /web\s+claude/
Show

# Step 1: the prompt, in web.
Alt+w
Sleep 1s
Type "The /orders endpoint started returning 422 for me. Figure out why and fix our client."
Sleep 500ms
Enter

# Steps 2-5: find, ask, the idle api answers, web asks for run_tests. The TUI card reads:
#   approval needed (1/1)
#   web → api  run_tests
#   "<task>"
#    o once   s session   a always   d deny
Wait+Screen@300s /approval needed/
Sleep 3s

# Pick the bottom pane and answer `a` (always), as in the SPEC: the grant stays listed in the
# `g` view at the end. record.sh wipes the database per take, so it cannot leak into the next one.
Alt+t
Sleep 1s
Type "a"

# Step 6: tests run in api, web applies the fix. The activity feed line becomes
#   HH:MM:SS  web → api  run_tests  ● done  "<task>"
Wait+Screen@300s /web → api\s+run_tests\s+● done/
# web is woken by the reply and finishes its answer.
Sleep 15s
# End on the TUI: two agents and a completed request, then (key `g`) the approved grant.
Type "g"
Sleep 5s
