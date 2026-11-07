# Contributing to Covey

Thanks for looking. Covey is small on purpose, so a short read of the rules below saves a round trip.

## Set up

```
uv sync --all-extras
uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Tests use ephemeral sockets, so a test run does not touch a daemon you have running. Both the tests and ruff must pass before a pull request.

The design is in [docs/SPEC.md](docs/SPEC.md). Read the section for the module you are changing. If the spec and the code disagree, say so in the issue or PR instead of picking one.

## The rules

- The daemon never calls a model and never needs an API key. It coordinates agents that have their own.
- No hosted service, no accounts, no cloud registry. Everything runs on the user's machine.
- Covey is not an agent framework: no planners, memory stores or tool libraries here.
- Agents cannot grant themselves anything. Only the human token creates grants, and gated capabilities are re-checked when the reply arrives, not only when the request is created.
- A delegated task is always delivered inside a visible frame that says it comes from another agent, not the user. Do not remove that frame.
- Headless workers never run with `--dangerously-skip-permissions`, and spawn budgets are enforced.
- The card format and the nine MCP tool names are public API. Do not rename them casually.
- Prefer small, boring code. No plugin systems, no abstractions with one implementation.
- Every CLI command takes `--json` (except `covey top` and `covey mcp`), and every error message says what to do next.

## Schema first

`src/covey/schema/card.json`, `src/covey/schema/api.yaml` and the DDL in `src/covey/daemon/store.py` are the contract. Change them deliberately, in their own commit, with a migration if the database changes. Do not bundle a schema change with a feature.

## Tests

Write the failing test first and watch it fail for the right reason. Router, grants and ranking logic should stay near 90% covered. Do not assert on a mock instead of the real component, and do not pin assertions to an exact message string.

## Claude Code hooks

Do not claim a hook behaves a certain way from memory. Run it against a real Claude Code session, record the payload into `tests/fixtures/hooks/`, and check the installed version's hooks reference. The recorded fixtures are for Claude Code 2.1.319.

## Good first issues

These are scoped to a few hours each. Open the list: [good first issues](https://github.com/filip-a-9/covey/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

1. `covey top --json`: print one snapshot of the `ps` and approvals data, then exit.
2. `python -m covey`: add `src/covey/__main__.py` so the module runs like the `covey` command.
3. Ranking: add stemming (`remind` matches `reminders`) and a small synonym table, so the two expected failures in `tests/unit/test_registry.py` pass.
4. Show a "told <agent>" line in `covey top` when a reply wakes an idle asker.
5. A thin hook launcher to cut the 340 to 490 ms Python start per hook. Measure first, and report the numbers before writing code.

Comment on the issue before you start, so two people do not do the same one.

## Commits and pull requests

One commit per finished change, with a subject that names it and one short body line per distinct part. Describe the project change, nothing else.

## Reporting bugs

Use the issue templates. A bug report wants `covey doctor --json`; a missed delivery wants `covey log --json --limit 50`. For a security problem, do not open a public issue. Use GitHub's private vulnerability reporting on the repository.
