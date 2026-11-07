# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased] - 0.1.2

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

[Unreleased]: https://github.com/filip-a-9/covey/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/filip-a-9/covey/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/filip-a-9/covey/releases/tag/v0.1.0
