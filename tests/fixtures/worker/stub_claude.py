"""A stand-in for `claude -p --output-format json`, for the spawner's integration tests.

Run as `python stub_claude.py <claude argv...>` with:
    STUB_MODE  reply | silent-success | max-budget | crash | hang | ask-peer
    STUB_LOG   file to append one JSON line to: argv, stdin, cwd

`reply` and `ask-peer` start the covey MCP server exactly as `--mcp-config` says and call
its tools, so the worker shim, its token and the mcp.json command are all exercised.
Output shapes follow the recorded runs in claude-code-2.1.319/.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ULID = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def result(subtype: str = "success", text: str | None = "stub answer", **extra) -> str:
    out = {
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "session_id": "stub-session",
        "total_cost_usd": 0.01,
        "num_turns": 2,
        "duration_ms": 5,
        "permission_denials": [],
        **extra,
    }
    if text is not None:
        out["result"] = text
    return json.dumps(out)


async def call_tool(server: dict, tool: str, args: dict) -> str:
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=server["command"], args=server["args"], env=server["env"]
    )
    async with Client(params) as client:
        res = await client.call_tool(tool, args)
    text = res.content[0].text
    if res.is_error:
        raise SystemExit(f"{tool} failed: {text}")
    return text


def main() -> None:
    argv = sys.argv[1:]
    task = sys.stdin.read()
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(
            json.dumps(
                {"argv": argv, "stdin": task, "cwd": os.getcwd(), "pid": os.getpid()}
            )
            + "\n"
        )
    rid = ULID.search(task).group(0)
    config = json.loads(Path(flag(argv, "--mcp-config")).read_text())
    server = config["mcpServers"]["covey"]
    mode = os.environ["STUB_MODE"]
    if mode == "reply":
        asyncio.run(
            call_tool(server, "covey_reply", {"request_id": rid, "result": "via mcp"})
        )
        print(result(text="I replied with covey_reply."))
    elif mode == "silent-success":
        print(result(text="silent answer"))
    elif mode == "max-budget":
        print(result("error_max_budget_usd", None, errors=["Reached maximum budget"]))
        sys.exit(1)
    elif mode == "crash":
        print("Traceback (most recent call last): boom")
        sys.exit(1)
    elif mode == "hang":
        time.sleep(3600)
    elif mode == "ask-peer":
        asyncio.run(
            call_tool(
                server,
                "covey_ask",
                {"to": "web", "capability": "explain", "task": "what is X to you?"},
            )
        )
        print(result(text="asked web"))
    else:
        raise SystemExit(f"unknown STUB_MODE {mode!r}")


if __name__ == "__main__":
    main()
