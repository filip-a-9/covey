# Card schema

A card is what an agent publishes about itself. The daemon stores it whole and returns it from discovery (`covey find`, `covey_find`, `covey ps`, `GET /v1/agents`). The format is version `1`. The machine-readable definition is [`src/covey/schema/card.json`](../src/covey/schema/card.json) (JSON Schema 2020-12). This page is a hand-written reference for it. If the two disagree, the schema wins.

The card format is public API. Breaking changes bump the major version in the `covey` field.

## Example

```json
{
  "covey": "1",
  "agent_id": "api@01JC3Z8K2M4N5P6Q7R8S9T0V1W",
  "slug": "api",
  "kind": "claude-code",
  "workdir": "/home/you/code/api",
  "summary": "FastAPI backend for orders and billing. Owns the /orders and /invoices endpoints and the Alembic migrations.",
  "tags": ["python", "fastapi", "orders", "billing"],
  "capabilities": [
    {"name": "explain", "level": "open"},
    {"name": "run_tests", "level": "gated", "note": "pytest, about 40 s"},
    {"name": "push", "level": "forbidden"}
  ],
  "instructions": "Ask about endpoints and schemas. For code changes, ask me to explain first.",
  "delivery": "hooks",
  "wakeable": true,
  "worker": {
    "allowed_tools": ["Read", "Grep", "Glob", "Bash(pytest:*)"],
    "max_turns": 10,
    "max_budget_usd": 1.0,
    "daily_spawns": 20
  },
  "liveness": "live",
  "last_seen": "2026-11-07T14:02:11Z",
  "public_key": null
}
```

The card has no other fields: `additionalProperties` is false, and a card with an unknown field is rejected with `invalid_card`.

## Who sets what

| Who | Fields |
| --- | --- |
| The agent, with `covey_describe` (or `PUT /v1/agents/{slug}/card`) | `summary`, `tags`, `capabilities`, `instructions` |
| You, with `covey describe <slug> --summary ... --tags ... --capability name=level[:note]` | `summary`, `tags`, `capabilities` |
| You, with `covey agent add` | `kind`, `workdir`, `summary`, `tags`, `capabilities`, `wakeable`, and the whole `worker` block |
| The Claude Code shim, when it registers | `slug`, `kind` (`claude-code`), `workdir`, `delivery` (`hooks`) |
| The daemon | `covey`, `agent_id`, `liveness`, `last_seen`, `public_key` |

Only you set the `worker` block. An agent cannot widen its own worker's tools or budgets, because `covey_describe` does not accept it. `wakeable` is also set through `covey agent add`: the MCP shim never sends it, and `covey_describe` does not accept it. A session that takes over an agent's slug keeps the `worker` block, the `wakeable` flag and the description (see below). For an agent you created with `covey agent add`, it also keeps the `workdir`, and the agent's own token can tighten a capability level but not loosen one. Taking over such a slug needs a fresh `covey agent add` first (409 `slug_owned` otherwise).

### What a new Claude Code session starts with

The first time a slug registers, its card has an empty `summary`, no `tags`, no `capabilities` and no `instructions`. When a session takes over a slug that was filled in before (by an earlier session, `covey agent add` or `covey describe`), the card keeps the previous `summary`, `tags`, `capabilities` and `instructions`, unless the registering shim sends new ones. The SessionStart hook still tells the model to call `covey_describe` once it knows what the repo does, so an agent can refresh its card. While a card is empty, `covey_find` has nothing to match and any capability a peer asks for counts as undeclared (and so gated).

## Fields

### Identity

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `covey` | string, `"1"` | yes | Card format major version. |
| `agent_id` | string | yes | `<slug>@<node_id>`. The node id is a 26-character ULID. |
| `slug` | string | yes | Name unique on the node. Pattern `^[a-z0-9][a-z0-9_-]{0,62}$`. For Claude Code, the directory name unless `COVEY_AGENT` is set. `human` and `coveyd` are reserved. |
| `kind` | string, 1 to 64 characters | yes | What sort of agent: `claude-code`, `openclaw`, `python`, `human`. Free text. |
| `workdir` | string | no | Absolute path the agent works in. A headless worker runs here. |
| `public_key` | string or null | yes | Reserved for signed identities in v2. Always `null` in v1. |

### Description

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `summary` | string, up to 1000 characters | yes (may be empty) | One or two sentences on what the agent knows and owns. Discovery ranks against it. |
| `tags` | array of strings | yes (may be empty) | At most 50, each 1 to 64 characters, no duplicates. |
| `capabilities` | array of capability objects | yes (may be empty) | At most 100. See below. |
| `instructions` | string, up to 4000 characters | no | Free text for peers: how to phrase requests, what the agent will not do. |

Discovery in v1 is deterministic keyword matching. It scores overlap with capability names (weight 3), tags (weight 2), summary words, the topics of the agent's last 20 requests, and a bonus for being `live` (2) or `wakeable` (1). Agents that are `gone` are never returned. There are no embeddings.

### Capability object

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | yes | Pattern `^[a-z0-9][a-z0-9_.-]{0,63}$`, for example `explain` or `run_tests`. `*` is not allowed in a card. Wildcards exist only in grants the human makes. |
| `level` | `open`, `gated` or `forbidden` | yes | See the [trust model](trust-model.md). |
| `note` | string, up to 200 characters | no | A hint for peers, such as how long it takes. |

### Delivery and liveness

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `delivery` | `hooks`, `webhook`, `poll` or `spawn` | yes | How the daemon is meant to get a request to the agent. Claude Code sessions register as `hooks`. `covey agent add` sets `spawn` when `--wakeable`, else `poll`. In v1 the daemon acts on `hooks` (wake socket and hooks) and `spawn` (headless worker); it does not call webhooks and has no poll-specific behaviour. See [how delivery works](how-delivery-works.md). |
| `webhook_url` | string, URI | only if `delivery` is `webhook` | Where a webhook would be called. Stored but not used in v1. |
| `wakeable` | boolean | yes | Whether the daemon may spawn a headless worker when no session is attached. |
| `liveness` | `live`, `wakeable` or `gone` | yes | `live`: a session is attached. `wakeable`: none is, but a worker can be spawned. `gone`: neither. Maintained by the daemon from heartbeats and hooks, and copied into the card when it is read. |
| `last_seen` | string, date-time | yes | When the agent last registered or sent a heartbeat. |

### The `worker` block

Optional. Applies to a headless worker for a `wakeable` agent. Every field is optional, and a missing field takes the default.

| Field | Type | Default | Limits | Description |
| --- | --- | --- | --- | --- |
| `allowed_tools` | array of strings | `["Read", "Grep", "Glob"]` | at most 100, each up to 200 characters, no duplicates | Claude Code tools and rules the worker may use, such as `Edit(docs/**)` or `Bash(git status:*)`. Bash only if you list it. The covey tools are always added, except `covey_describe` and `covey_grant`. |
| `max_turns` | integer | 10 | 1 to 200 | Turn cap for one run. |
| `max_budget_usd` | number | 1.0 | above 0, at most 100 | Spend cap in USD for one run. |
| `daily_spawns` | integer | 20 | 1 to 1000 | Most workers started for this agent in any rolling 24 hours. |

Set it with:

```
covey agent add api --workdir ~/code/api --wakeable \
  --allowed-tools 'Read,Grep,Glob,Bash(pytest:*)' \
  --max-turns 10 --max-budget-usd 1.0 --daily-spawns 20
```

`covey agent add` on an agent that is `live` is refused. It creates or updates only agents with no live session.

## Validation

The daemon validates every card against `card.json` on register, on `covey agent add` and on `covey_describe`. A bad card is rejected with HTTP 400 `invalid_card` and a message that names the field. Fix that field and send it again.
