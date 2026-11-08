# Quickstart

From nothing to two Claude Code sessions talking, in about five minutes.

## Prerequisites

- Linux or macOS. Windows is best-effort until v1.1. On Windows, use WSL.
- [uv](https://docs.astral.sh/uv/).
- Claude Code, logged in. Idle wake needs 2.1.224 or newer. Covey is developed against 2.1.319.
- Two git repos that hold some code, to open sessions in. With empty directories the demo still runs, but the agent can only answer "I can't tell".

Covey itself needs no API key. The Claude Code sessions use their own.

## 1. Install

```
uv tool install 'covey-mesh[tui,notify]'
```

The package is `covey-mesh`; the command is `covey`.

The extras are optional. `tui` gives you `covey top`, and `notify` gives you desktop notifications for approvals. Without `tui`, `covey top` says so and exits; `covey top --json` still works.

### From source

To run an unreleased checkout, or a repository URL:

```
uv tool install --from '/path/to/covey[tui,notify]' covey-mesh
uv tool install --from git+https://github.com/filip-a-9/covey covey-mesh
```

Do not use `uvx`. The hooks need a `covey` that stays installed, and `covey init` refuses to run from uvx's temporary environment.

## 2. Wire Claude Code to Covey

```
covey init --user
```

This adds four hooks (SessionStart, Stop, PostToolUse, SessionEnd) to `~/.claude/settings.json` and a `covey` MCP server to `~/.claude.json`. Every Claude Code session you start from now on joins the mesh. It is safe to run again: a second run prints `Already set up: nothing changed.`

The alternative is project scope:

```
cd ~/code/api
covey init
```

This writes `.claude/settings.json` and `.mcp.json` in the current directory, for that project only. Warning: it writes the absolute path of your `covey` into `.mcp.json`. Do not commit that file, because it will not work on a teammate's machine. Run it in each repo you want in the mesh.

Both forms print the files they changed. Both ask you to restart any Claude Code sessions that are already open.

If you use a non-default `COVEY_HOME` or `COVEY_PORT`, set it in Claude Code's environment too. See the last item in [Troubleshooting](#troubleshooting).

## 3. Start the daemon and check it

```
covey up
covey doctor
```

`covey up` prints `coveyd is running (pid ..., port 7777).` Sessions also start the daemon on their own if it is not running, so this step mostly lets you see problems early.

`covey doctor` prints `ok` or `FAIL` for the socket, the tokens, the human token, the hooks, the MCP entry, `claude`, the `claude` lookup the daemon would make, the notification backend and the port. Every failure comes with a `fix:` line. When everything is `ok`, go on. See Troubleshooting below otherwise.

## 4. Open two sessions

```
cd ~/code/api && claude
cd ~/code/web && claude      # in a second terminal
```

Each session registers as an agent named after its directory: `api` and `web`. To pick another name, set `COVEY_AGENT=<slug>` in the covey MCP server's `env`.

A name you created with `covey agent add` or gave a grant belongs to you. A session may use it only if it starts from that agent's workdir (or a directory below it) and outside any other agent's session; otherwise the daemon refuses with 409 `slug_owned`. See the troubleshooting list.

The first session for a name starts with an empty card: no summary, no tags, no capabilities. Later sessions keep whatever the card already says. The SessionStart hook only tells the model its name ("You are agent `api` in the covey."); it does not ask for a card. Set the card yourself with `covey describe` or `covey agent add`; a session with an empty card sees "(none yet…)" in `covey_whoami`:

```
covey describe api --summary "Orders API: FastAPI, Postgres, Alembic migrations" --tags orders,billing --capability explain=open --capability run_tests=gated
covey ps
```

`--capability name=level[:note]` (or `covey_describe`) declares capabilities and replaces the whole list. Without a declared capability, a request for it needs your approval, because Covey treats an undeclared capability as gated. Step 5 asks for `explain`, which is now open, so it needs no approval. Step 6 asks for `run_tests`, which stays gated.

Check that both are there:

```
covey ps
covey find "who owns the orders API"
```

## 5. Ask one to ask the other

In the `web` session, type something like:

```
The /orders call returns 422. Ask the api agent what changed.
```

The model calls `covey_find`, then `covey_ask`, then `covey_wait`. In the `api` session, even if it is idle, Covey posts a message that starts a turn. The message says it is a request from another agent and not from you. The model reads the request with `covey_inbox`, answers with `covey_reply`, and the answer comes back to `web`.

You can also test an agent without a second session:

```
covey ask api "What changed in the last commit?" --wait
```

## 6. Approve a gated request

Ask for something with side effects:

```
Ask the api agent to run its tests.
```

If `run_tests` is gated, the request waits as `awaiting_grant`. The asking session tells you. Open the approval screen in a third terminal:

```
covey top
```

The approval appears as a card. Press `o` to allow this one request, `s` for the target's current session, `a` to always allow it, or `d` to deny. Other keys: `/` filter, `enter` open a request, `g` grants, `l` full log, `q` quit.

Without the TUI:

```
covey approvals
covey approve <request_id>              # once, the default
covey approve <request_id> --session
covey approve <request_id> --always
covey deny <request_id>                 # also refuses later matching requests, until `covey revoke <grant_id>`
```

If `covey top` is not open, you get a desktop notification when one is available (needs the `notify` extra, or WSL with `powershell.exe` on PATH). Otherwise the daemon only logs it. Run these commands in your own terminal. The daemon refuses them from inside a Claude Code session.

Grants can also be made ahead of time:

```
covey grant web api run_tests --session
covey grants
covey revoke <grant_id>
```

## 7. Read the log

```
covey log
covey log --thread <thread_id>
covey log --agent api --since 2026-11-07T00:00:00Z
covey log --json
covey log --follow
```

`covey log` prints a transcript of requests grouped by thread, with each request's audit events (approvals, wake posts, deliveries, worker spawns) beneath it. `covey log --follow` keeps printing new events as they happen. `covey log --json` also includes events that aren't about a request, such as agent and grant rows. Every command except `covey mcp` (a stdio server) takes `--json`. `covey top --json` prints one snapshot and exits without opening the TUI, so it works without the `tui` extra. It has four keys: `covey` (the API version), `agents` (the cards `covey ps` lists), `requests` (the newest 200 requests in any status, oldest first) and `approvals` (the requests waiting for you, as `covey approvals` lists them).

## 8. Stop

```
covey down
```

## The scripted demo

[examples/two-repos/README.md](../examples/two-repos/README.md) builds two throwaway repos, wires them up and runs this scenario in tmux. It starts real Claude Code sessions and costs money.

## Troubleshooting

Start with `covey doctor`. Each failure below is a line it can print.

| Doctor says | What to do |
| --- | --- |
| `FAIL socket: no coveyd on ...` | Run `covey up`. If it fails, read the log path it prints (`coveyd.log` in `~/.covey`). |
| `FAIL tokens: ... is missing` | Run `covey up`. It creates them. |
| `FAIL tokens: ... is mode ..., not 600` | Run the `chmod 600` commands doctor prints. |
| `FAIL human token: ...` | Doctor ran inside an agent's process tree, or `COVEY_HOME` points at another daemon's files. Run it from your own terminal. |
| `FAIL hooks: missing: ...` | Run `covey init --user` (or `covey init` in the project). Doctor looks at the current directory and your home directory. A hook whose `covey` path no longer exists counts as missing. |
| `FAIL mcp: no mcpServers.covey entry` | Run `covey init --user` (or `covey init` in the project). |
| `FAIL claude: ...` | Install Claude Code and put `claude` on PATH. |
| `FAIL claude for coveyd: ...` | Set `COVEY_CLAUDE` to the full path from `which claude`, then `covey down && covey up`. Only headless workers need this. |
| `FAIL notify backend: ...` | Start `coveyd` from a shell where `powershell.exe` is on PATH (WSL), or set `COVEY_NOTIFY=log`, then `covey down && covey up`. |
| `FAIL port: ... in use by something else` | Stop what uses the port, or set `COVEY_PORT` to a free port, then `covey up`. |

Other things you may meet:

- **`covey init` says it is running from uvx's temporary environment.** Install with `uv tool install` (see step 1) and run `covey init --user` again.
- **`covey top` says it needs the tui extra.** Reinstall with the `tui` extra (step 1).
- **A tool answers "Another session already holds the agent name ...".** Two live sessions have the same directory name. Set `COVEY_AGENT=<other-slug>` in one session's covey MCP server `env` and restart that session.
- **A tool answers 409 `slug_owned`.** The name was created with `covey agent add` (or has grants) and this session did not start in its workdir, or it started inside another agent's session. Start `claude` from the workdir, or run `covey agent add <slug> --workdir <dir>` if the agent moved on purpose. An SDK client over TCP cannot take such a name at all.
- **A request fails with 413, 429 or 403 `bad_host`.** A body over 1 MiB gets 413; `task`, `context` and `result` are limited to 64 KiB and `artifacts` to 32 items, so put large content in a file and pass the path. A requester may have 10 requests waiting for approval; answer some with `covey approve` or `covey deny`. Over TCP the `Host` header must be `127.0.0.1`, `localhost` or `[::1]` with the port, so do not reach the daemon through a proxy or tunnel.
- **The session says coveyd is unreachable.** Ask for `covey up` in your terminal, then retry the tool.
- **After `covey down` and `covey up`.** Open sessions keep working; the next tool call reconnects. If one does not, restart that Claude Code session.
- **Hooks seem to do nothing.** Restart the Claude Code session after `covey init`. Hooks and MCP servers load at session start.
- **A request stays `queued` and the other session does not react.** See [how delivery works](how-delivery-works.md), "When nothing happens". `covey inbox <agent>` shows what is waiting and does not mark anything delivered.
- **`covey ask` or `covey_ask` fails with "gone".** The target has no session and is not wakeable. Open a session there, or register it with `covey agent add <slug> --workdir <repo> --wakeable`.
- **A different `COVEY_PORT` or `COVEY_HOME`.** Both are read from the environment by every `covey` command and by the daemon. Hooks and the shim run inside Claude Code, so set them in that environment too.
