"""A minimal Covey agent: joins the mesh and answers `echo` requests.

Run `covey up`, then `uv run python examples/python-agent/agent.py`. From a terminal:
`covey ask pyagent "hello" --cap echo`.
"""

from covey.sdk import Agent


def handle(request: dict) -> str:
    """Called once per request; the return value is the reply. Raising sends `failed`."""
    return f"pyagent heard: {request['task']}"


def make_agent(**kw) -> Agent:
    return Agent(
        "pyagent",
        summary="Echoes whatever it is asked. A demo of the Python SDK.",
        capabilities=[{"name": "echo", "level": "open"}],
        **kw,
    )


if __name__ == "__main__":
    print("pyagent is on the mesh; Ctrl-C to leave")
    try:
        make_agent().serve(handle)
    except KeyboardInterrupt:
        pass
