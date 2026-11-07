# Roadmap

This is a list of intent, not a promise or a schedule. Covey 0.1 is the first release; what comes next depends on what people ask for.

## Next: v1.1

- Windows support: named pipes in place of the unix socket, and process lookup. Until then Covey runs on Linux and macOS (Windows through WSL).
- The `webhook` delivery mode. It is in the card format already, but nothing acts on it yet.
- Cross-node peering: `covey peer add <host>` with a shared secret, LAN discovery, and request forwarding between daemons.
- An OpenClaw adapter (a recipe plus webhook delivery), and recipes for other MCP clients such as Cursor, Codex CLI and OpenCode, including a `covey init --client` flag. Non-Claude-Code agents can already join through the SDK.

## Later: v2, only if v1 gets traction

- Per-agent keypairs, signed requests, and trust levels per peer node.
- An A2A edge: `coveyd` serves an A2A Agent Card and accepts A2A tasks for its agents.
- Budgets per request and per thread (tokens and spawns), with the TUI showing spend.
- Shared scratch space: a per-thread directory for exchanging files.
- A semantic ranker as an optional plugin, for people who want embeddings.

## Ideas we are watching

- A `UserPromptSubmit` hook as another delivery point.
- Refreshing the wake stamp on session activity, instead of a fixed 10 minute re-offer. We will revisit this if a real lost wake shows up.
- Claude Code Channels and `asyncRewake` as fallback delivery. They matter only if the current idle wake stops working.
- Re-attaching a shim to its session after a restart.

`poll` delivery stays as it is by design: the agent pulls its own work, so there is nothing to build.

## Not planned

Covey will not run models or need an API key, grow into an agent framework, become a hosted service with accounts or a marketplace, or act as a general message bus for applications.

Want to help? See [CONTRIBUTING.md](CONTRIBUTING.md).
