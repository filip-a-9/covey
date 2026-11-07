"""api.yaml against the running daemon: the spec is valid, and every implemented operation's
real responses match the schemas the spec declares for that path, method and status."""

import json
from pathlib import Path

import httpx
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "src" / "covey" / "schema"
BASE = SCHEMA_DIR.as_uri() + "/"
SPEC = yaml.safe_load((SCHEMA_DIR / "api.yaml").read_text())
NOT_IMPLEMENTED = {
    "streamEvents",  # SSE moves to the TUI slice
    "createAgent",  # spawner slice 2 (covey agent add)
}


def operations() -> set[tuple[str, str]]:
    return {
        (path, method)
        for path, item in SPEC["paths"].items()
        for method, op in item.items()
        if op["operationId"] not in NOT_IMPLEMENTED
    }


def registry() -> Registry:
    return Registry().with_resources(
        [
            (BASE + "api.yaml", Resource.from_contents(SPEC, DRAFT202012)),
            (
                BASE + "card.json",
                Resource.from_contents(
                    json.loads((SCHEMA_DIR / "card.json").read_text()), DRAFT202012
                ),
            ),
        ]
    )


def test_api_yaml_is_a_valid_openapi_document():
    validate(SPEC, base_uri=BASE)


def response_schema(path: str, method: str, status: int) -> dict:
    response = SPEC["paths"][path][method]["responses"][str(status)]
    if "$ref" in response:
        name = response["$ref"].rsplit("/", 1)[1]
        response = SPEC["components"]["responses"][name]
    inner = response["content"]["application/json"]["schema"]
    if "$ref" in inner:
        return {"$ref": BASE + "api.yaml" + inner["$ref"]}
    return inner


class Checker:
    def __init__(self):
        self.seen: set[tuple[str, str]] = set()
        self.registry = registry()

    def call(self, client, method, template, status, path_params=None, **kw):
        url = template.format(**(path_params or {}))
        r = client.request(method, url, **kw)
        assert r.status_code == status, f"{method} {url}: {r.status_code} {r.text}"
        schema = response_schema(template, method, status)
        validator = Draft202012Validator(
            schema, registry=self.registry, format_checker=FormatChecker()
        )
        validator.validate(r.json())
        self.seen.add((template, method))
        return r.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_every_implemented_operation_matches_its_declared_response(daemon):
    chk = Checker()
    tcp = httpx.Client(base_url=daemon.base_url, timeout=15)
    uds = httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path),
        base_url="http://covey",
        headers=bearer(daemon.human_token),
        timeout=15,
    )
    join = bearer(daemon.join_token)
    reg = "/v1/agents/register"
    caps = [
        {"name": "explain", "level": "open"},
        {"name": "run_tests", "level": "gated"},
    ]

    def body(slug):
        return {
            "slug": slug,
            "kind": "python",
            "delivery": "poll",
            "capabilities": caps,
        }

    # agents
    web_token = chk.call(tcp, "post", reg, 200, json=body("web"), headers=join)[
        "agent_token"
    ]
    api_token = chk.call(tcp, "post", reg, 200, json=body("api"), headers=join)[
        "agent_token"
    ]
    web, api = bearer(web_token), bearer(api_token)
    chk.call(tcp, "post", reg, 400, json={"slug": "Bad Slug"}, headers=join)
    chk.call(tcp, "post", reg, 401, json=body("x"))
    chk.call(tcp, "post", reg, 409, json=body("web"), headers=join)

    hb = "/v1/agents/{slug}/heartbeat"
    chk.call(tcp, "post", hb, 200, {"slug": "web"}, headers=web)
    chk.call(tcp, "post", hb, 401, {"slug": "web"})
    chk.call(tcp, "post", hb, 404, {"slug": "ghost"}, headers=join)

    chk.call(tcp, "get", "/v1/agents", 200, params={"need": "explain"}, headers=web)
    chk.call(tcp, "get", "/v1/agents", 401)

    card = "/v1/agents/{slug}/card"
    chk.call(tcp, "put", card, 200, {"slug": "web"}, json={"summary": "s"}, headers=web)
    chk.call(tcp, "put", card, 400, {"slug": "web"}, json={}, headers=web)
    chk.call(tcp, "put", card, 401, {"slug": "web"}, json={"summary": "s"})
    chk.call(tcp, "put", card, 403, {"slug": "api"}, json={"summary": "s"}, headers=web)
    chk.call(uds, "put", card, 404, {"slug": "ghost"}, json={"summary": "s"})

    inbox = "/v1/agents/{slug}/inbox"
    chk.call(tcp, "get", inbox, 200, {"slug": "api"}, headers=api)
    chk.call(tcp, "get", inbox, 401, {"slug": "api"})
    chk.call(tcp, "get", inbox, 403, {"slug": "api"}, headers=web)
    chk.call(uds, "get", inbox, 404, {"slug": "ghost"})

    chk.call(tcp, "get", "/v1/whoami", 200, headers=web)
    chk.call(tcp, "get", "/v1/whoami", 401)

    sess = "/v1/agents/{slug}/session"
    sid = {"session_id": "s1"}
    chk.call(tcp, "put", sess, 200, {"slug": "web"}, json=sid, headers=web)
    chk.call(tcp, "put", sess, 400, {"slug": "web"}, json={}, headers=web)
    chk.call(tcp, "put", sess, 401, {"slug": "web"}, json=sid)
    chk.call(tcp, "put", sess, 403, {"slug": "api"}, json=sid, headers=web)
    tmp = bearer(
        chk.call(tcp, "post", reg, 200, json=body("tmp"), headers=join)["agent_token"]
    )
    end = {"reason": "logout"}
    chk.call(tcp, "delete", sess, 200, {"slug": "tmp"}, json=end, headers=tmp)
    chk.call(tcp, "delete", sess, 400, {"slug": "tmp"}, json={}, headers=tmp)
    chk.call(tcp, "delete", sess, 401, {"slug": "tmp"}, json=end)
    chk.call(tcp, "delete", sess, 403, {"slug": "api"}, json=end, headers=tmp)
    stop = "/v1/agents/{slug}/inbox/stop-claim"
    chk.call(tcp, "post", stop, 200, {"slug": "api"}, headers=api)
    chk.call(tcp, "post", stop, 401, {"slug": "api"})
    chk.call(tcp, "post", stop, 403, {"slug": "api"}, headers=web)

    # requests
    ask = {"to": "api", "capability": "explain", "task": "t"}
    created = chk.call(tcp, "post", "/v1/requests", 201, json=ask, headers=web)
    rid = created["request"]["request_id"]
    chk.call(tcp, "post", "/v1/requests", 400, json={"to": "api"}, headers=web)
    chk.call(tcp, "post", "/v1/requests", 401, json=ask)
    chk.call(tcp, "post", "/v1/requests", 404, json={**ask, "to": "ghost"}, headers=web)
    parent = rid
    for hop in range(4):  # hops 1..4 are allowed, the next one is not
        sender, to = (api, "web") if hop % 2 == 0 else (web, "api")
        step = {**ask, "to": to, "parent_id": parent}
        parent = chk.call(tcp, "post", "/v1/requests", 201, json=step, headers=sender)[
            "request"
        ]["request_id"]
    chk.call(
        tcp, "post", "/v1/requests", 422, json={**ask, "parent_id": parent}, headers=web
    )

    one = "/v1/requests/{request_id}"
    chk.call(tcp, "get", one, 200, {"request_id": rid}, headers=web)
    chk.call(tcp, "get", one, 401, {"request_id": rid})
    other = chk.call(tcp, "post", reg, 200, json=body("other"), headers=join)
    chk.call(
        tcp, "get", one, 403, {"request_id": rid}, headers=bearer(other["agent_token"])
    )
    chk.call(tcp, "get", one, 404, {"request_id": "0" * 26}, headers=web)

    rep = one + "/reply"
    done = {"result": "r", "status": "done"}
    chk.call(tcp, "post", rep, 200, {"request_id": rid}, json=done, headers=api)
    chk.call(
        tcp, "post", rep, 400, {"request_id": rid}, json={"result": "r"}, headers=api
    )
    chk.call(tcp, "post", rep, 401, {"request_id": rid}, json=done)
    chk.call(tcp, "post", rep, 403, {"request_id": rid}, json=done, headers=web)
    chk.call(tcp, "post", rep, 404, {"request_id": "0" * 26}, json=done, headers=api)
    chk.call(
        tcp,
        "post",
        rep,
        409,
        {"request_id": rid},
        json={"result": "different", "status": "done"},
        headers=api,
    )

    # grants and approvals
    gbody = {"requester": "web", "target": "api", "capability": "x", "scope": "always"}
    chk.call(tcp, "get", "/v1/grants", 200, headers=web)
    chk.call(tcp, "get", "/v1/grants", 401)
    grant = chk.call(uds, "post", "/v1/grants", 201, json=gbody)["grant"]
    chk.call(uds, "post", "/v1/grants", 400, json={**gbody, "scope": "bogus"})
    chk.call(tcp, "post", "/v1/grants", 401, json=gbody)
    chk.call(tcp, "post", "/v1/grants", 403, json=gbody, headers=web)
    chk.call(uds, "post", "/v1/grants", 404, json={**gbody, "target": "ghost"})
    g = "/v1/grants/{grant_id}"
    chk.call(tcp, "delete", g, 401, {"grant_id": grant["grant_id"]})
    chk.call(tcp, "delete", g, 403, {"grant_id": grant["grant_id"]}, headers=web)
    chk.call(uds, "delete", g, 200, {"grant_id": grant["grant_id"]})
    chk.call(uds, "delete", g, 404, {"grant_id": grant["grant_id"]})

    gated = {**ask, "capability": "run_tests"}
    pending = chk.call(tcp, "post", "/v1/requests", 201, json=gated, headers=web)
    pid = pending["request"]["request_id"]
    assert pending["request"]["status"] == "awaiting_grant"
    chk.call(uds, "get", "/v1/approvals", 200)
    chk.call(tcp, "get", "/v1/approvals", 401)
    chk.call(tcp, "get", "/v1/approvals", 403, headers=web)
    a = "/v1/approvals/{request_id}"
    chk.call(uds, "post", a, 400, {"request_id": pid}, json={"scope": "bogus"})
    chk.call(tcp, "post", a, 401, {"request_id": pid}, json={"scope": "once"})
    chk.call(
        tcp, "post", a, 403, {"request_id": pid}, json={"scope": "once"}, headers=web
    )
    chk.call(uds, "post", a, 404, {"request_id": "0" * 26}, json={"scope": "once"})
    chk.call(uds, "post", a, 200, {"request_id": pid}, json={"scope": "once"})
    chk.call(uds, "post", a, 409, {"request_id": pid}, json={"scope": "once"})

    # log
    chk.call(tcp, "get", "/v1/log", 200, headers=web)
    chk.call(tcp, "get", "/v1/log", 401)

    missing = operations() - chk.seen
    assert not missing, f"operations never validated: {sorted(missing)}"
    tcp.close()
    uds.close()
