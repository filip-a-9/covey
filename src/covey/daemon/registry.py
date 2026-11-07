"""Registry: cards, liveness and discovery ranking.

Every function takes an open connection and joins the caller's transaction if there is one.
The `agents.liveness` and `agents.last_seen` columns are authoritative; `card_of()` returns
the stored card with those two fields copied in. Errors are `CoveyError`.

Rules for this slice (docs/SPEC.md "Data model & wire protocol" and "Core concepts"):
- Register builds the full card from the RegisterBody and validates it against
  `schema/card.json` (400 `invalid_card`). Slugs `human` and `coveyd` are reserved (409 `slug_reserved`).
  A slug whose holder is `live` is refused (409 `slug_live`); otherwise the caller takes it
  over. Every register mints a new agent token; only its sha256 is stored.
- Liveness: no heartbeat for `timeout_s` -> `wakeable` if the card says wakeable, else `gone`.
  `session_id` is kept: session grants match only while the agent is `live`, and a heartbeat
  from the same session revives them. A new register replaces the session id. The human row
  never changes.
- Ranking, per SPEC.md: score = 3 x capability name match + 2 x tag overlap
  + 1 x BM25-ish token overlap on summary + 1 x overlap with the agent's last 20 request
  topics + liveness bonus (live 2, wakeable 1, gone excluded). Each match has a one-line `why`.
"""

import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from covey.daemon import peer, store
from covey.daemon.errors import CoveyError

# src/covey/schema has no __init__.py, so find it relative to this file.
_CARD_SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "card.json"
_VALIDATOR = Draft202012Validator(json.loads(_CARD_SCHEMA.read_text()))

# session_id after endAgentSession. It is "" (never a real id, and a session grant cannot
# carry one) rather than NULL, so a never-attached agent that the sweep made wakeable can
# still be revived by a heartbeat while an ended one cannot. No schema change needed.
ENDED_SESSION = ""
DESCRIBABLE = ("summary", "tags", "capabilities", "instructions")
RECENT_REQUESTS = 20
# Tiny synonym table for discovery ranking (one place). Keys are stemmed tokens.
SYNONYMS = {
    "payment": "billing",
}


STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "get",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "this",
        "to",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)


def card_of(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """The stored card with `liveness` and `last_seen` taken from the row's columns."""
    card = json.loads(row["card"])
    card["liveness"] = row["liveness"]
    card["last_seen"] = row["last_seen"]
    return card


def get_agent(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """The agents row for `slug` as a dict, or 404 `agent_not_found`."""
    row = conn.execute("SELECT * FROM agents WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise CoveyError(
            404,
            "agent_not_found",
            f"No agent named {slug!r} on this node.",
            "List agents with `covey ps` or GET /v1/agents.",
        )
    return dict(row)


def agent_by_token_hash(
    conn: sqlite3.Connection, token_hash: str
) -> dict[str, Any] | None:
    """The agents row whose token_hash equals `token_hash`, or None."""
    row = conn.execute(
        "SELECT * FROM agents WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    return dict(row) if row else None


def _validate(card: dict[str, Any]) -> None:
    error = next(iter(_VALIDATOR.iter_errors(card)), None)
    if error is not None:
        where = "/".join(str(p) for p in error.path) or "card"
        raise CoveyError(
            400,
            "invalid_card",
            f"Invalid card at {where}: {error.message}",
            "Fix the field against src/covey/schema/card.json and register again.",
        )


def _check_not_reserved(slug: str) -> None:
    if slug in store.RESERVED_SLUGS:
        raise CoveyError(
            409,
            "slug_reserved",
            f"The slug {slug!r} is reserved for covey itself.",
            "Pick another slug.",
        )


def _existing_not_live(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """The agents row for `slug` if any; 409 `slug_live` if its holder is live."""
    row = conn.execute("SELECT * FROM agents WHERE slug = ?", (slug,)).fetchone()
    if row is not None and row["liveness"] == "live":
        raise CoveyError(
            409,
            "slug_live",
            f"An agent named {slug!r} is live on this node.",
            "Pick another slug, or wait for the other session to end.",
        )
    return row


LEVEL_ORDER = {"open": 0, "gated": 1, "forbidden": 2}


def _human_created(conn: sqlite3.Connection, agent_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM audit WHERE event = 'agent.created' AND subject = ?",
            (agent_id,),
        ).fetchone()
        is not None
    )


def _inside(cwd: str, workdir: str) -> bool:
    try:
        return Path(cwd).resolve().is_relative_to(Path(workdir).resolve())
    except OSError:
        return False


def _check_takeover_allowed(
    conn: sqlite3.Connection,
    agent_id: str,
    slug: str,
    old: dict[str, Any],
    origin: peer.Origin | None,
) -> None:
    """A join-token register on a slug the human created or that holds grants (either
    direction) would hand the taker the slug's trust. It is allowed only from outside the
    process tree of every other agent, and, if the human set a workdir, from inside that
    directory. An unidentifiable peer (TCP, exited) or cwd is refused. Derived from the
    audit log and grants, so no schema change."""
    granted = conn.execute(
        "SELECT 1 FROM grants WHERE requester = ? OR target = ?", (agent_id, agent_id)
    ).fetchone()
    human = _human_created(conn, agent_id)
    if not (granted or human):
        return
    workdir = old.get("workdir") if human else None
    if origin is None:
        problem = "Cannot tell which process is registering"
    elif origin.agents - {agent_id}:
        problem = "This session runs inside another agent's process tree"
    elif workdir and (origin.cwd is None or not _inside(origin.cwd, workdir)):
        problem = f"This session was not started from {workdir}"
    else:
        return
    where = f"start the session from {workdir}, outside any other agent's session, "
    raise CoveyError(
        409,
        "slug_owned",
        f"{problem}, and the agent {slug!r} was set up by the human or holds grants, "
        "so it cannot be taken over from here.",
        (where if workdir else "start the session outside any other agent's session, ")
        + f"or run `covey agent add {slug} --workdir <dir>` if the agent moved on "
        "purpose; or pick another slug.",
    )


def _keep_human_levels(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """`new` capabilities, except that a capability already in `old` keeps the stricter of
    the two levels and one `new` leaves out is kept: an agent tightens, never loosens."""
    by_name = {c["name"]: c for c in new}
    for cap in old:
        mine = by_name.get(cap["name"])
        if mine is None or LEVEL_ORDER[cap["level"]] > LEVEL_ORDER[mine["level"]]:
            by_name[cap["name"]] = {**(mine or {}), **cap}
    return list(by_name.values())


def check_workdir(workdir: str | None, covey_home: Path) -> None:
    """A workdir that is the user's home or contains COVEY_HOME would let a worker read the
    daemon's tokens."""
    if not workdir:
        return
    path = Path(workdir).expanduser().resolve()
    home = covey_home.resolve()
    if path == Path.home().resolve() or path == home or path in home.parents:
        raise CoveyError(
            400,
            "bad_workdir",
            f"{path} is your home or contains {home}, so a worker could read the "
            "daemon's tokens.",
            "Pass the agent's repo as the workdir (--workdir <repo>).",
        )


def create_agent(
    conn: sqlite3.Connection, node_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Pre-register an agent with no session (covey agent add), or update the card of one
    that is not live. Mints no token and keeps an existing one. Liveness is `wakeable` or
    `gone` by `body["wakeable"]`. The only way to set the card's `worker` block. Audits
    `agent.created` as the human. `body` is a CreateAgentBody."""
    slug = body.get("slug", "")
    _check_not_reserved(slug)
    agent_id = f"{slug}@{node_id}"
    now = store.now_iso()
    wakeable = bool(body.get("wakeable", False))
    liveness = "wakeable" if wakeable else "gone"
    card: dict[str, Any] = {
        "covey": "1",
        "agent_id": agent_id,
        "slug": slug,
        "kind": body.get("kind", "claude-code"),
        "summary": body.get("summary", ""),
        "tags": body.get("tags", []),
        "capabilities": body.get("capabilities", []),
        # Nothing is attached yet; a session that takes the slug over sets its own.
        "delivery": "spawn" if wakeable else "poll",
        "wakeable": wakeable,
        "liveness": liveness,
        "last_seen": now,
        "public_key": None,
        "workdir": body.get("workdir"),
    }
    for optional in ("instructions", "worker"):
        if optional in body:
            card[optional] = body[optional]
    _validate(card)

    with store.transaction(conn):
        row = _existing_not_live(conn, slug)
        if row is None:
            conn.execute(
                "INSERT INTO agents (agent_id, slug, node_id, card, liveness, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, slug, node_id, json.dumps(card), liveness, now),
            )
        else:
            card["last_seen"] = row["last_seen"]
            conn.execute(
                "UPDATE agents SET card = ?, liveness = ? WHERE slug = ?",
                (json.dumps(card), liveness, slug),
            )
        store.audit(
            conn,
            f"human@{node_id}",
            "agent.created",
            agent_id,
            {"wakeable": wakeable, "update": row is not None},
        )
    return card


def register(
    conn: sqlite3.Connection,
    node_id: str,
    body: dict[str, Any],
    origin: peer.Origin | None = None,
) -> tuple[dict[str, Any], str]:
    """Register or take over a slug. `origin` is where the call came from (None if unknown),
    judged only for a slug the human owns. Returns (card, agent_token). Audits
    `agent.registered` (never with the token). `body` is a RegisterBody; `session_id` goes to the column, not
    the card."""
    slug = body.get("slug", "")
    _check_not_reserved(slug)
    agent_id = f"{slug}@{node_id}"
    now = store.now_iso()
    card: dict[str, Any] = {
        "covey": "1",
        "agent_id": agent_id,
        "slug": slug,
        "kind": body.get("kind"),
        "summary": body.get("summary", ""),
        "tags": body.get("tags", []),
        "capabilities": body.get("capabilities", []),
        "delivery": body.get("delivery"),
        "wakeable": body.get("wakeable"),
        "liveness": "live",
        "last_seen": now,
        "public_key": None,
    }
    for optional in ("workdir", "instructions", "webhook_url"):
        if optional in body:
            card[optional] = body[optional]

    token = secrets.token_urlsafe(32)
    token_hash = store.hash_token(token)
    session_id = body.get("session_id")
    with store.transaction(conn):
        row = _existing_not_live(conn, slug)
        old = json.loads(row["card"]) if row is not None else {}
        if row is not None:
            _check_takeover_allowed(conn, agent_id, slug, old, origin)
        # A takeover keeps what only the human sets: the worker block, and wakeable
        # unless the body says otherwise (covey agent add, SPEC "Delivery").
        if row is not None and _human_created(conn, agent_id):
            if "workdir" in old:
                card["workdir"] = old["workdir"]
            else:
                card.pop("workdir", None)
            card["capabilities"] = _keep_human_levels(
                old.get("capabilities", []), card["capabilities"]
            )
        if card["wakeable"] is None:
            card["wakeable"] = old.get("wakeable", False)
        if "worker" in old:
            card["worker"] = old["worker"]
        # Likewise the description: a shim sends none, and a new session must not
        # blank what covey agent add / covey describe set. A field in the body wins.
        for field in DESCRIBABLE:
            if field not in body and field in old:
                card[field] = old[field]
        _validate(card)
        if row is None:
            conn.execute(
                "INSERT INTO agents (agent_id, slug, node_id, card, liveness, last_seen,"
                " session_id, token_hash) VALUES (?, ?, ?, ?, 'live', ?, ?, ?)",
                (
                    agent_id,
                    slug,
                    node_id,
                    json.dumps(card),
                    now,
                    session_id,
                    token_hash,
                ),
            )
        else:
            conn.execute(
                "UPDATE agents SET card = ?, liveness = 'live', last_seen = ?,"
                " session_id = ?, token_hash = ? WHERE slug = ?",
                (json.dumps(card), now, session_id, token_hash, slug),
            )
        store.audit(
            conn,
            agent_id,
            "agent.registered",
            agent_id,
            {
                "kind": card["kind"],
                "delivery": card["delivery"],
                "takeover": row is not None,
            },
        )
    return card, token


def heartbeat(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """Mark the agent live now. Returns {"liveness", "last_seen"}. 404 for an unknown slug.
    The human row is never heartbeated: it is treated as unknown (404)."""
    if slug == "human":
        raise CoveyError(
            404,
            "agent_not_found",
            "The human pseudo-agent has no heartbeat.",
            "Heartbeat as a registered agent.",
        )
    agent = get_agent(conn, slug)
    now = store.now_iso()
    # After end_session the session_id is ENDED_SESSION: refresh last_seen, do not revive.
    attached = agent["session_id"] != ENDED_SESSION
    with store.transaction(conn):
        if attached:
            conn.execute(
                "UPDATE agents SET liveness = 'live', last_seen = ? WHERE slug = ?",
                (now, slug),
            )
            if agent["liveness"] != "live":
                store.audit(
                    conn,
                    agent["agent_id"],
                    "agent.liveness",
                    agent["agent_id"],
                    {"from": agent["liveness"], "to": "live"},
                )
        else:
            conn.execute("UPDATE agents SET last_seen = ? WHERE slug = ?", (now, slug))
    return {"liveness": "live" if attached else agent["liveness"], "last_seen": now}


def set_session(conn: sqlite3.Connection, slug: str, session_id: str) -> dict[str, Any]:
    """Attach `session_id` and mark the agent live. A changed id ends the old session's
    `session` grants (they match only the current id). Audits `agent.session_started`."""
    agent = get_agent(conn, slug)
    now = store.now_iso()
    with store.transaction(conn):
        conn.execute(
            "UPDATE agents SET session_id = ?, liveness = 'live', last_seen = ?"
            " WHERE slug = ?",
            (session_id, now, slug),
        )
        store.audit(
            conn,
            agent["agent_id"],
            "agent.session_started",
            agent["agent_id"],
            {"changed": agent["session_id"] != session_id},
        )
    return {"liveness": "live", "last_seen": now}


def end_session(conn: sqlite3.Connection, slug: str, reason: str) -> dict[str, Any]:
    """Mark the session ended (session_id becomes ENDED_SESSION, which matches no grant and
    tells heartbeat not to revive); liveness becomes `wakeable` or `gone` by the card, as in the
    sweep. Audits `agent.session_ended`."""
    agent = get_agent(conn, slug)
    new = "wakeable" if json.loads(agent["card"])["wakeable"] else "gone"
    with store.transaction(conn):
        conn.execute(
            "UPDATE agents SET session_id = ?, liveness = ? WHERE slug = ?",
            (ENDED_SESSION, new, slug),
        )
        store.audit(
            conn,
            agent["agent_id"],
            "agent.session_ended",
            agent["agent_id"],
            {"reason": reason, "to": new},
        )
    return {"liveness": new, "last_seen": agent["last_seen"]}


def describe(
    conn: sqlite3.Connection,
    slug: str,
    fields: dict[str, Any],
    by_agent: bool = False,
) -> dict[str, Any]:
    """Update summary / tags / capabilities / instructions; re-validate; return the card.
    `by_agent`: the agent's own token, which on a human-created slug may tighten a level
    but never loosen one. Audits `agent.described`."""
    row = get_agent(conn, slug)
    card = card_of(row)
    changed = {k: fields[k] for k in DESCRIBABLE if k in fields}
    if by_agent and "capabilities" in changed and _human_created(conn, row["agent_id"]):
        changed["capabilities"] = _keep_human_levels(
            card["capabilities"], changed["capabilities"]
        )
    card.update(changed)
    _validate(card)
    with store.transaction(conn):
        conn.execute(
            "UPDATE agents SET card = ? WHERE slug = ?", (json.dumps(card), slug)
        )
        store.audit(
            conn,
            row["agent_id"],
            "agent.described",
            row["agent_id"],
            {"fields": sorted(changed)},
        )
    return card


def whoami(conn: sqlite3.Connection, agent_id: str, node_id: str) -> dict[str, Any]:
    """{"card", "node_id", "liveness"} for the caller."""
    row = conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise CoveyError(
            404,
            "agent_not_found",
            f"No agent {agent_id}.",
            "Register again with the join token.",
        )
    return {"card": card_of(row), "node_id": node_id, "liveness": row["liveness"]}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)  # 3.11 accepts the trailing Z


def sweep(conn: sqlite3.Connection, now: str, timeout_s: float) -> list[str]:
    """Apply the liveness timeout. Returns the agent_ids whose liveness changed; audits
    `agent.liveness` for each."""
    cutoff = _parse(now) - timedelta(seconds=timeout_s)
    rows = conn.execute(
        "SELECT agent_id, node_id, card, last_seen FROM agents"
        " WHERE liveness = 'live' AND slug != 'human'"
    ).fetchall()
    changed = []
    with store.transaction(conn):
        for row in rows:
            if _parse(row["last_seen"]) >= cutoff:
                continue
            new = "wakeable" if json.loads(row["card"])["wakeable"] else "gone"
            conn.execute(
                "UPDATE agents SET liveness = ? WHERE agent_id = ?",
                (new, row["agent_id"]),
            )
            store.audit(
                conn,
                store.daemon_actor(row["node_id"]),
                "agent.liveness",
                row["agent_id"],
                {"from": "live", "to": new},
            )
            changed.append(row["agent_id"])
    return changed


def _stem(word: str) -> str:
    """Tiny deterministic stemmer: strip one common suffix, keep a 4-letter root."""
    for suffix in ("ing", "ed", "er"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric words, minus stopwords, with a light plural strip,
    a tiny stemmer and a small synonym expansion."""
    out = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        word = _stem(word)
        out.append(word)
        syn = SYNONYMS.get(word)
        if syn is not None:
            out.append(_stem(syn))
    return out


def _bm25(
    query: set[str], doc: list[str], docs: list[list[str]], k1=1.5, b=0.75
) -> tuple[float, list[str]]:
    """BM25 of `doc` for the query tokens, and the query tokens that matched."""
    avg_len = sum(len(d) for d in docs) / len(docs) or 1
    score, hits = 0.0, []
    for term in sorted(query):
        tf = doc.count(term)
        if tf == 0:
            continue
        df = sum(1 for d in docs if term in d)
        idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
        score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
        hits.append(term)
    return score, hits


def find(
    conn: sqlite3.Connection,
    need: str | None = None,
    capability: str | None = None,
    limit: int | None = 5,
) -> list[dict[str, Any]]:
    """Discovery. Returns [{"card", "score", "why"}], best first, at most `limit` (None: no cap).

    The human pseudo-agent and `gone` agents are never returned. `capability` keeps only
    agents that declare it at a level other than forbidden. Without `need`, all remaining
    agents, unranked (score 0, ordered by slug).
    """
    rows = conn.execute(
        "SELECT * FROM agents WHERE slug != 'human' AND liveness != 'gone' ORDER BY slug"
    ).fetchall()
    cards = [card_of(r) for r in rows]
    if capability is not None:
        cards = [
            c
            for c in cards
            if any(
                cap["name"] == capability and cap["level"] != "forbidden"
                for cap in c["capabilities"]
            )
        ]
    if not need:
        return [{"card": c, "score": 0, "why": ""} for c in cards[:limit]]

    query = set(_tokens(need))
    summaries = [_tokens(c["summary"]) for c in cards]
    matches = []
    for card, summary in zip(cards, summaries, strict=True):
        why, relevance = [], 0.0
        caps = [
            c["name"] for c in card["capabilities"] if query & set(_tokens(c["name"]))
        ]
        cap_hits = {t for n in caps for t in _tokens(n)} & query
        if caps:
            relevance += 3 * len(cap_hits)
            why.append("capability " + ", ".join(caps))
        tags = [t for t in card["tags"] if query & set(_tokens(t))]
        tag_hits = {tok for t in tags for tok in _tokens(t)} & query
        if tags:
            relevance += 2 * len(tag_hits)
            why.append("tags: " + ", ".join(tags))
        score, words = _bm25(query, summary, summaries)
        if words:
            relevance += score
            why.append("summary: " + ", ".join(words))
        topics = conn.execute(
            "SELECT task FROM requests WHERE to_agent = ? ORDER BY created_at DESC,"
            " request_id DESC LIMIT ?",
            (card["agent_id"], RECENT_REQUESTS),
        ).fetchall()
        seen = {t for r in topics for t in _tokens(r["task"])} & query
        if seen:
            relevance += len(seen)
            why.append("recent requests: " + ", ".join(sorted(seen)))
        if relevance == 0:
            continue
        bonus = 2 if card["liveness"] == "live" else 1
        matches.append(
            {
                "card": card,
                "score": round(relevance + bonus, 3),
                "why": "; ".join(why),
            }
        )
    matches.sort(key=lambda m: (-m["score"], m["card"]["slug"]))
    return matches[:limit]
