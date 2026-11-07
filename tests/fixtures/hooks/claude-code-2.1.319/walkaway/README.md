# Walk-away runs (SPEC open question 1)

Two real Claude Code 2.1.319 sessions, `api` and `web`, idle in tmux with nobody at the
keyboard, on Linux (WSL2), daemon `covey-mesh` 0.1.1. A script outside this repo asks `web`
"reply with the word pong" with `--cap explain` as the human through the `covey` CLI. At the end it reads
`covey log --json --since <start>` and records, for each request, the delivery path,
the number of `wake_released` re-offers, and the time from ask to reply.

## Results

| Run | Date | Shape | Replied | Pending | Median | Max | Path |
|---|---|---|---|---|---|---|---|
| 1 | 2026-11-07 | 18 asks, every 10 min for 3 h | 18/18 | 0 | 4.7 s | 5.6 s | wake post, 0 re-offers |
| 2 | 2026-11-07 | 8 asks, 1 min apart, 20-min freeze after ask 3 | 8/8 | 0 | 4.8 s | 5.6 s | wake post, 0 re-offers |

**Run 1:** every request was delivered by the wake post and replied to on the first offer.
Ask-to-reply ranged from 4.3 s to 5.6 s, with no drift over the three hours.

**Run 2:** the host can't do a real suspend: it has S0 Modern Standby only, with no S3 sleep
and no reliable wake timer. So run 2 is a proxy. coveyd and both `claude` process trees were
stopped with `SIGSTOP` for 20 minutes, then resumed with `SIGCONT`. That tests coveyd's own
sweep, timeout and re-offer logic across a 20-minute gap where neither side runs. It does not
test WSL's clock or network after a real host resume. The run was compressed to the gap
itself (3 asks a minute apart, the freeze, then 5 asks a minute apart), because run 1 already
covers the three hours.

The 3 asks before the freeze replied in 4.6 to 4.8 s. Ask 4 went out the moment the
processes resumed and replied in 5.6 s. Asks 5 to 8 replied in 4.5 to 5.6 s. Nothing was
re-offered and nothing stayed pending.

**Still open:** delivery across a real laptop sleep. To check it, start the same script with
no action at hour 1 before a normal lid close.
