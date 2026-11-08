# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-08

### Changed

- The database moves to schema version 2 on first start, and an older covey cannot open it afterwards. Back up `~/.covey/covey.db` before upgrading if you may need to go back.
- The join token can no longer heartbeat. A shim from 0.1.x gets 403: restart each Claude Code session after upgrading.
- A session registering on a slug that the human created or granted must run inside that agent's workdir and outside every other agent's process tree; otherwise it gets 409 `slug_owned`.
- A `once` grant is tied to the request it authorized.
- Idle wake posts only to a socket inside `$XDG_RUNTIME_DIR/cc-socks`. On macOS, set `COVEY_WAKE_SOCKET_DIR` to the directory Claude Code uses.
- The build backend is pinned to hatchling 1.33.0.

### Security

- Request limits: an HTTP body over 1 MiB gets 413; `task`, `context` and `result` are at most 64 KiB each; a requester may have at most 10 requests waiting for approval.
- Over TCP, the `Host` header must name `127.0.0.1`, `localhost` or `[::1]` with the daemon's port.
- A workdir that contains the user's home or the daemon's home is refused with 400 `bad_workdir`.
- The human-token ancestry check is harder to escape, and a headless worker's token is scoped to its own request.
- Agent-written text is escaped in the CLI and `covey top`, and framed as peer text in discovery, Stop and SessionStart output.
- Capability names with a trailing newline are rejected.
- The daemon no longer serves `/openapi.json` or `/docs`.

## [0.1.3] - 2026-11-07

### Fixed

- The daemon gives each thread its own database connection. Before, one request's rollback could drop another request's committed write, and on Python 3.13 parallel tool calls could fail with a server error.

### Changed

- `covey agent add --help` and the delivery docs say that worker turn and spend caps are checked after each turn, so a worker can go one turn over.
- The quickstart works when followed word for word.

## [0.1.2] - 2026-11-07

### Fixed

- Stop-hook delivery survives re-prompts (`stop_hook_active`) and lost hooks, so a request is no longer dropped in those cases.
- A race in the events stream test.

### Changed

- `covey doctor` warns when the installed Claude Code is not the tested 2.1.319, and `--json` carries `version` and `tested`.
- `wakeable` is set by the human only (`covey agent add --wakeable`); registering with the join token ignores it.
- Docs match 0.1.x: install instructions, `--json` coverage, and the CLI table.

### Added

- A clear refusal on Windows that points to WSL, instead of a raw error.
- Tests for `covey deny` end to end, TUI approval keypresses, the wake timeout and each grant scope, and more CI coverage.
- CONTRIBUTING, ROADMAP, this changelog and GitHub issue templates.

## [0.1.1] - 2026-11-07

### Added

- `covey log` prints approvals, deliveries and worker events under each request, and `covey log --follow` streams new events as text or JSON lines.
- A desktop notification when a request targets an agent that is gone.

### Fixed

- `covey init`'s refusal under uvx names the scope you ran in.
- `covey top`'s missing-extra hint names `uv tool install`.

## [0.1.0] - 2026-11-07

First release, published to PyPI as `covey-mesh`.

### Added

- `coveyd`: registry, router, grants and waker over SQLite, with an HTTP API, human and agent tokens, and an SSE event stream.
- The `covey mcp` stdio shim with nine tools and two resources.
- Claude Code integration: `covey init`, four hooks, and delivery to idle sessions through the messaging socket, the Stop hook and the PostToolUse hook.
- Headless worker spawning with spawn budgets.
- Capability levels, grant scopes, and approvals in `covey top`, the CLI and desktop notifications.
- Deterministic discovery ranking, threads, hop limits and request deadlines.
- The Python SDK, `covey top`, `covey doctor`, the scripted two-repo hero demo, and the docs.
- CI on Linux and macOS, and a tag-driven release workflow.

[0.2.0]: https://github.com/filip-a-9/covey/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/filip-a-9/covey/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/filip-a-9/covey/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/filip-a-9/covey/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/filip-a-9/covey/releases/tag/v0.1.0
