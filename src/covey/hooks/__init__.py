"""Claude Code hook handlers, one small module per event.

Each module has `run(payload, ctx) -> dict | None`: `payload` is the hook's stdin JSON and
the return value is the JSON Claude Code should see on stdout (None prints nothing).
`covey hook <event>` (cli/hook.py) wires them up and never lets an error reach the session.
"""
