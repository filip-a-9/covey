# Now

**Current slice:** core daemon + HTTP API.

**Done:** schemas (`card.json`, `api.yaml`, `store.py` DDL) and the trust decisions in SPEC.md.

**Blocked:** how hooks authenticate (SPEC.md, "Decisions still yours"). It depends on whether
the shim can learn Claude Code's `session_id`. This does not block the daemon slice, but it
blocks the hooks slice.

**Learned this week:** the hook questions in SPEC.md ("Open technical questions") are still
untested on Claude Code 2.1.319. Nothing about hook semantics is assumed until a real session
has been recorded into `tests/fixtures/hooks/`.
