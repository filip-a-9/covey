#!/usr/bin/env bash
# usage: setup.sh <dir>
# Builds throwaway repos <dir>/api and <dir>/web, wires Covey into each, and registers
# their cards in a private COVEY_HOME (<dir>/.covey). Safe to run again.
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: setup.sh <dir>" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$1"
DIR="$(cd "$1" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

# Never the user's real ~/.covey, never the default port.
export COVEY_HOME="$DIR/.covey"
export COVEY_PORT="${COVEY_PORT:-7788}"

cat > "$DIR/env.sh" <<EOF
export COVEY_HOME="$COVEY_HOME"
export COVEY_PORT="$COVEY_PORT"
export COVEY_BIN="$COVEY_BIN"
export PATH="$(dirname "$COVEY_BIN"):\$PATH"
EOF

# Fixed identity for the demo repos only; the global git config is never read or written.
export GIT_AUTHOR_NAME="Demo" GIT_AUTHOR_EMAIL="demo@example.invalid"
export GIT_COMMITTER_NAME="Demo" GIT_COMMITTER_EMAIL="demo@example.invalid"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

days_ago() {
  date -d "$1 days ago" '+%Y-%m-%dT10:00:00' 2>/dev/null || date -v-"$1"d '+%Y-%m-%dT10:00:00'
}

# commit <days ago> <message...>: stage everything and commit with that date.
commit() {
  local when
  when="$(days_ago "$1")"
  shift
  git add -A
  GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" git commit -q -m "$*"
}

make_api() {
  local d="$DIR/api"
  [ -d "$d/.git" ] && return 0
  mkdir -p "$d" && cd "$d"
  git init -q -b main
  cat > CLAUDE.md <<'EOF'
# api

The orders backend. Other agents may ask you about it through Covey. Answer from
your own git log and code, and reply with the reason, not just the diff.
Run the tests with `python3 -m unittest`. When asked to run the tests, run exactly that,
report pass or fail, and do nothing else.
EOF
  cat > server.py <<'EOF'
"""Tiny orders backend (stdlib only). Run: python3 server.py"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from orders import validate_order

ORDERS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/orders":
            return self.reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.reply(400, {"error": "invalid json"})
        errors = validate_order(payload)
        if errors:
            return self.reply(422, {"errors": errors})
        ORDERS.append(payload)
        self.reply(201, {"id": len(ORDERS), **payload})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
EOF
  cat > orders.py <<'EOF'
"""Request schema for POST /orders."""


def validate_order(payload):
    """Return a list of error strings; empty means the payload is valid."""
    errors = []
    if not isinstance(payload.get("sku"), str) or not payload["sku"]:
        errors.append("sku is required")
    return errors
EOF
  cat > test_orders.py <<'EOF'
import unittest

from orders import validate_order


class ValidateOrder(unittest.TestCase):
    def test_accepts_a_sku(self):
        self.assertEqual(validate_order({"sku": "A-1"}), [])

    def test_rejects_a_missing_sku(self):
        self.assertEqual(validate_order({}), ["sku is required"])


if __name__ == "__main__":
    unittest.main()
EOF
  commit 9 "Add orders endpoint with sku validation"

  python3 - <<'EOF'
import re
p = open("orders.py").read()
p += '''

def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
'''
p = p.replace(
    "    return errors\n",
    '    if "quantity" in payload and not _positive_int(payload["quantity"]):\n'
    '        errors.append("quantity must be a positive integer")\n    return errors\n',
    1,
)
open("orders.py", "w").write(p)
t = open("test_orders.py").read()
t = t.replace(
    "\n\nif __name__",
    '''
    def test_rejects_a_zero_quantity(self):
        errors = validate_order({"sku": "A-1", "quantity": 0})
        self.assertEqual(errors, ["quantity must be a positive integer"])


if __name__''',
    1,
)
open("test_orders.py", "w").write(t)
EOF
  commit 6 "Validate order quantity"

  sed -i 's/ORDERS.append(payload)/ORDERS.append(payload)  # in memory only; no database yet/' server.py
  printf '__pycache__/\n.claude/\n.mcp.json\n' > .gitignore
  commit 4 "Ignore caches and local tooling files"

  python3 - <<'EOF'
p = open("orders.py").read()
p = p.replace(
    '    if "quantity" in payload',
    '    if not isinstance(payload.get("currency"), str) or len(payload["currency"]) != 3:\n'
    '        errors.append("currency is required (ISO 4217 code, e.g. EUR)")\n'
    '    if "quantity" in payload',
    1,
)
open("orders.py", "w").write(p)
t = open("test_orders.py").read()
t = t.replace('{"sku": "A-1"}), [])', '{"sku": "A-1", "currency": "EUR"}), [])')
t = t.replace(
    '{"sku": "A-1", "quantity": 0}',
    '{"sku": "A-1", "currency": "EUR", "quantity": 0}',
)
t = t.replace(
    'validate_order({}), ["sku is required"])',
    'validate_order({"currency": "EUR"}), ["sku is required"])',
)
t = t.replace(
    "\n\nif __name__",
    '''
    def test_rejects_a_missing_currency(self):
        errors = validate_order({"sku": "A-1"})
        self.assertEqual(errors, ["currency is required (ISO 4217 code, e.g. EUR)"])


if __name__''',
    1,
)
open("test_orders.py", "w").write(t)
EOF
  commit 1 "Require currency on POST /orders

Orders were stored without a currency, so finance reports mixed EUR and USD
totals. POST /orders now requires an ISO 4217 currency code; a request
without one is rejected with 422."
  cd "$DIR"
}

make_web() {
  local d="$DIR/web"
  [ -d "$d/.git" ] && return 0
  mkdir -p "$d" && cd "$d"
  git init -q -b main
  cat > CLAUDE.md <<'EOF'
# web

The client for the orders backend. The backend lives in another repo and is owned
by another agent: do not read its files. When the backend misbehaves, do not explore
this repo first: find that agent with Covey (`covey_find`), ask it what changed
(`covey_ask`), then read `client.py`. Before you finish a fix, ask it to run its test suite against your
change (capability `run_tests`).
Run this repo's tests with `python3 -m unittest`.
EOF
  cat > client.py <<'EOF'
"""Client for the orders backend (stdlib only)."""

import json
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


def build_order_payload(sku, quantity=1):
    return {"sku": sku, "quantity": quantity}


def send_order(sku, quantity=1, base_url=BASE_URL):
    """POST an order; return (status, decoded body)."""
    req = urllib.request.Request(
        f"{base_url}/orders",
        data=json.dumps(build_order_payload(sku, quantity)).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        return err.code, json.load(err)
EOF
  cat > test_client.py <<'EOF'
import unittest

from client import build_order_payload


class BuildOrderPayload(unittest.TestCase):
    def test_carries_sku_and_quantity(self):
        payload = build_order_payload("A-1", 3)
        self.assertEqual(payload["sku"], "A-1")
        self.assertEqual(payload["quantity"], 3)


if __name__ == "__main__":
    unittest.main()
EOF
  printf '__pycache__/\n.claude/\n.mcp.json\n' > .gitignore
  commit 8 "Add orders client"
  cd "$DIR"
}

# Project-scope `covey init`, then merge permissions into what it wrote (keeps its hooks).
wire_repo() {
  local d="$DIR/$1"
  (cd "$d" && "$COVEY_BIN" init >/dev/null)
  python3 - "$d/.claude/settings.json" <<'EOF'
import json, sys

path = sys.argv[1]
settings = json.load(open(path))
# "mcp__covey" allows every tool of the MCP server named `covey` (the name `covey init`
# writes into .mcp.json). The rest keeps Claude Code's own prompts off the screen.
allow = [
    "mcp__covey",
    "Read",
    "Edit",
    "Grep",
    "Glob",
    "Bash(ls:*)",
    "Bash(tail:*)",
    "Bash(head:*)",
    "Bash(cat:*)",
    "Bash(git ls-files:*)",
    "Bash(python3:*)",
    "Bash(PYTHONPATH=. python3:*)",
    "Bash(python3 -m unittest:*)",
    "Bash(python -m unittest:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
]
perms = settings.setdefault("permissions", {})
have = perms.setdefault("allow", [])
have.extend(a for a in allow if a not in have)
# Approves the project's .mcp.json server without Claude Code's one-time prompt.
settings["enableAllProjectMcpServers"] = True
open(path, "w").write(json.dumps(settings, indent=2) + "\n")
EOF
}

make_api
make_web
wire_repo api
wire_repo web

"$COVEY_BIN" up >/dev/null
# `agent add` refuses a slug whose session is live (rerun while a demo is open): fine.
"$COVEY_BIN" agent add api --workdir "$DIR/api" >/dev/null || echo "note: api not re-added (live?)" >&2
"$COVEY_BIN" agent add web --workdir "$DIR/web" >/dev/null || echo "note: web not re-added (live?)" >&2
# The cards survive a session registering, so they are set once, here.
"$COVEY_BIN" describe api --summary "$API_SUMMARY" --tags "$API_TAGS" "${API_CAPS[@]}" >/dev/null
"$COVEY_BIN" describe web --summary "$WEB_SUMMARY" --tags "$WEB_TAGS" "${WEB_CAPS[@]}" >/dev/null

echo "Demo ready in $DIR (COVEY_HOME=$COVEY_HOME, port $COVEY_PORT). Next: layout.sh $DIR"
