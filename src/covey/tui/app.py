"""`covey top`: agents, activity and approvals on one screen, live from GET /v1/events.

Colour is meaning only: green done/live, amber waiting/wakeable, red failed or denied, grey
gone, blue the selected row. Never ANSI dim. Events are only a trigger: each burst of them
re-reads agents, log and approvals, so the screen always shows what the daemon says.
"""

import asyncio
import json
from datetime import datetime
from typing import Any, ClassVar

import httpx
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from covey.config import Config
from covey.text import safe

GREEN, AMBER, RED, GREY, BLUE = "#3fb950", "#d9a521", "#f85149", "#8a8a8a", "#58a6ff"
LIVENESS_COLOUR = {"live": GREEN, "wakeable": AMBER, "gone": GREY}
STATUS_COLOUR = {
    "done": GREEN,
    "failed": RED,
    "refused": RED,
    "expired": RED,
    "awaiting_grant": AMBER,
    "queued": AMBER,
    "delivered": AMBER,
    "working": AMBER,
    "needs_input": AMBER,
}
FINAL = ("done", "failed", "refused", "expired")
ALL = "*"
SCOPE_KEYS = {"o": "once", "s": "session", "a": "always", "d": "deny"}


def slug_of(agent_id: str) -> str:
    return agent_id.partition("@")[0]


def clock(iso: str) -> str:
    """HH:MM:SS in local time from an ISO-8601 UTC timestamp."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso[11:19]


def open_counts(requests: list[dict], slug: str) -> tuple[int, int]:
    """(open requests into `slug`, open requests out of it)."""
    live = [r for r in requests if r["status"] not in FINAL]
    inbound = sum(slug_of(r["to_agent"]) == slug for r in live)
    outbound = sum(slug_of(r["from_agent"]) == slug for r in live)
    return inbound, outbound


def fit(text: Text, width: int) -> Text:
    """Cut each line of `text` to `width` cells with an ellipsis (OptionList would wrap)."""
    if width < 10:
        return text
    lines = text.split("\n")
    for line in lines:
        line.truncate(width, overflow="ellipsis")
    return Text("\n").join(lines)


def agent_row(card: dict, requests: list[dict]) -> Text:
    inbound, outbound = open_counts(requests, card["slug"])
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append("● ", style=LIVENESS_COLOUR.get(card["liveness"], GREY))
    line.append(card["slug"], style="bold")
    line.append(f" {safe(card.get('kind', ''))[:6]}  ")
    line.append(f"{inbound}↓ {outbound}↑", style=AMBER if inbound or outbound else "")
    line.append("\n  " + safe(card.get("summary", "")))
    return line


def request_line(req: dict) -> Text:
    colour = STATUS_COLOUR.get(req["status"], "")
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(clock(req["created_at"]) + "  ")
    line.append(
        f"{slug_of(req['from_agent'])} → {slug_of(req['to_agent'])}", style="bold"
    )
    line.append(f"  {req['capability']}  ")
    line.append(f"● {req['status']}", style=colour)
    line.append(f'  "{safe(req["task"])}"')
    return line


def reply_line(req: dict) -> Text | None:
    if not req.get("result"):
        return None
    first = safe(req["result"].strip().splitlines()[0]) if req["result"].strip() else ""
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(
        f"          ↳ {slug_of(req['to_agent'])}: ",
        style=STATUS_COLOUR.get(req["status"], ""),
    )
    line.append(first)
    return line


def approval_card(req: dict, position: int, total: int) -> Text:
    card = Text()
    card.append("approval needed ", style=f"bold {AMBER}")
    card.append(f"({position}/{total})\n", style=AMBER)
    card.append(
        f"{slug_of(req['from_agent'])} → {slug_of(req['to_agent'])}", style="bold"
    )
    card.append(f"  {req['capability']}\n")
    card.append(f'"{safe(req["task"])}"\n', style="italic")
    for key, scope in SCOPE_KEYS.items():
        card.append(f" {key} ", style=f"bold reverse {RED if key == 'd' else GREEN}")
        card.append(f" {scope}  ")
    return card


class TextScreen(ModalScreen):
    """A read-only page (transcript, grants, full log). Escape or q closes it."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape,q", "app.pop_screen", "close")]

    def __init__(self, title: str, lines: list[Text]):
        super().__init__()
        self.heading, self.lines = title, lines

    def compose(self) -> ComposeResult:
        yield RichLog(id="page", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        page = self.query_one(RichLog)
        page.border_title = self.heading
        for line in self.lines or [Text("Nothing here yet.")]:
            page.write(line)
        page.focus()


class CoveyTop(App):
    TITLE = "covey top"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #agents { width: 35%; border: round $primary; }
    #right { width: 1fr; }
    #feed { height: 1fr; border: round $primary; }
    #approvals { height: auto; min-height: 3; max-height: 8; border: round $primary; padding: 0 1; }
    #approvals.pending { border: round #d9a521; }
    #filter { display: none; height: 3; }
    #filter.show { display: block; }
    OptionList { background: transparent; }
    OptionList > .option-list--option-highlighted { background: #1f4e8c; color: white; }
    #page { border: round $primary; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("slash", "filter", "filter", key_display="/"),
        Binding("enter", "open", "open"),
        Binding("g", "grants", "grants"),
        Binding("l", "full_log", "log"),
        Binding("o", "answer('once')", "once"),
        Binding("s", "answer('session')", "session"),
        Binding("a", "answer('always')", "always"),
        Binding("d", "answer('deny')", "deny"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.http: httpx.AsyncClient | None = None
        self.agents: list[dict] = []
        self.requests: list[dict] = []
        self.pending: list[dict] = []
        self.shown_approval: str | None = None  # the request id the card is showing
        self.selected = ALL
        self.text_filter = ""
        self.wake = asyncio.Event()

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield OptionList(id="agents")
            with Vertical(id="right"):
                yield Input(placeholder="filter activity", id="filter")
                yield OptionList(id="feed")
                yield Static(id="approvals")
        yield Footer()

    async def on_mount(self) -> None:
        token = self.cfg.token_path.read_text().strip()
        self.http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.cfg.socket_path)),
            base_url="http://covey",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, read=None),
        )
        self.query_one("#agents").border_title = "agents"
        self.query_one("#feed").border_title = "activity"
        self.query_one("#approvals").border_title = "approvals"
        self.query_one("#agents").focus()
        await self.refresh_data()
        self.run_worker(self.follow_events(), exclusive=False)
        self.run_worker(self.refresh_loop(), exclusive=False)

    async def on_unmount(self) -> None:
        if self.http:
            await self.http.aclose()

    # data -------------------------------------------------------------------------------

    async def get(self, path: str, **params: Any) -> dict:
        assert self.http is not None
        r = await self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def refresh_data(self) -> None:
        try:
            agents = await self.get("/v1/agents")
            log = await self.get("/v1/log", limit=200)
            approvals = await self.get("/v1/approvals")
        except httpx.HTTPError:
            self.sub_title = "daemon unreachable; retrying"
            return
        self.sub_title = ""
        self.agents = [m["card"] for m in agents["matches"]]
        self.requests = log["requests"]
        self.pending = approvals["requests"]
        self.render_all()

    async def follow_events(self) -> None:
        """Stream /v1/events; every frame wakes the refresher. Reconnects on failure."""
        assert self.http is not None
        while True:
            try:
                async with self.http.stream("GET", "/v1/events") as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if line.startswith("data:"):
                            self.wake.set()
            except httpx.HTTPError:
                self.sub_title = "event stream lost; retrying"
            await asyncio.sleep(2)

    async def refresh_loop(self) -> None:
        while True:
            await self.wake.wait()
            await asyncio.sleep(0.1)  # let a burst of events settle
            self.wake.clear()
            await self.refresh_data()

    # rendering --------------------------------------------------------------------------

    def visible_requests(self) -> list[dict]:
        shown = self.requests
        if self.selected != ALL:
            shown = [
                r
                for r in shown
                if self.selected in (slug_of(r["from_agent"]), slug_of(r["to_agent"]))
            ]
        if self.text_filter:
            needle = self.text_filter.lower()
            shown = [r for r in shown if needle in request_line(r).plain.lower()]
        return shown

    def on_resize(self) -> None:
        if self.agents or self.requests:
            self.render_all()

    def render_all(self) -> None:
        self.render_agents()
        self.render_feed()
        self.render_approvals()

    def render_agents(self) -> None:
        box = self.query_one("#agents", OptionList)
        width = box.size.width - 2
        keep = self.selected
        everyone = Text("all agents", style="bold")
        options = [Option(everyone, id=ALL)]
        options += [
            Option(fit(agent_row(c, self.requests), width), id=c["slug"])
            for c in self.agents
        ]
        box.clear_options()
        box.add_options(options)
        ids = [o.id for o in options]
        box.highlighted = ids.index(keep) if keep in ids else 0

    def render_feed(self) -> None:
        box = self.query_one("#feed", OptionList)
        width = box.size.width - 2
        current = box.highlighted_option.id if box.highlighted_option else None
        options: list[Option] = []
        for req in self.visible_requests():
            options.append(Option(fit(request_line(req), width), id=req["request_id"]))
            reply = reply_line(req)
            if reply:
                options.append(Option(fit(reply, width), id=None, disabled=True))
        box.clear_options()
        box.add_options(options)
        ids = [o.id for o in options]
        if current in ids:
            box.highlighted = ids.index(current)
        elif ids:
            box.highlighted = len(options) - 1 if options[-1].id else len(options) - 2

    def render_approvals(self) -> None:
        bar = self.query_one("#approvals", Static)
        bar.set_class(bool(self.pending), "pending")
        if not self.pending:
            self.shown_approval = None
            bar.update(Text("nothing waiting for you"))
            return
        self.shown_approval = self.pending[0]["request_id"]
        card = approval_card(self.pending[0], 1, len(self.pending))
        bar.update(card)

    # events from widgets and keys -------------------------------------------------------

    @on(OptionList.OptionHighlighted, "#agents")
    def agent_chosen(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and event.option.id != self.selected:
            self.selected = event.option.id
            self.render_feed()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.text_filter = event.value.strip()
        self.render_feed()

    @on(Input.Submitted, "#filter")
    def filter_done(self) -> None:
        self.query_one("#feed").focus()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("show")
        box.focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            box = self.query_one("#filter", Input)
            if box.has_focus or self.text_filter:
                box.value = ""
                box.remove_class("show")
                self.query_one("#feed").focus()

    async def action_answer(self, scope: str) -> None:
        if (
            self.query_one("#filter", Input).has_focus
            or not self.shown_approval
            or not self.http
        ):
            return
        r = await self.http.post(
            f"/v1/approvals/{self.shown_approval}", json={"scope": scope}
        )
        if r.status_code >= 400:
            message = r.json().get("error", {})
            self.notify(
                safe(
                    f"{message.get('message', r.reason_phrase)} "
                    f"{message.get('next_step', '')}"
                ),
                severity="error",
            )
        self.wake.set()

    async def action_open(self) -> None:
        box = self.query_one("#feed", OptionList)
        if not box.has_focus or box.highlighted_option is None:
            return
        rid = box.highlighted_option.id
        req = next((r for r in self.requests if r["request_id"] == rid), None)
        if req is None:
            return
        found = await self.get("/v1/log", thread=req["thread_id"])
        lines: list[Text] = []
        for item in found["requests"]:
            lines += [request_line(item), Text("    task: " + safe(item["task"]))]
            if item.get("context"):
                lines.append(Text("    context: " + safe(item["context"])))
            if item.get("result"):
                lines.append(Text("    result: " + safe(item["result"])))
            lines.append(Text(""))
        self.push_screen(TextScreen(f"thread {req['thread_id']}", lines))

    async def action_grants(self) -> None:
        found = await self.get("/v1/grants")
        lines = [
            Text(
                f"{slug_of(g['requester'])} → {slug_of(g['target'])}  {g['capability']}  "
                f"{g['scope']}  {clock(g['created_at'])}"
            )
            for g in found["grants"]
        ]
        self.push_screen(TextScreen("grants", lines))

    async def action_full_log(self) -> None:
        found = await self.get("/v1/log", limit=500)
        lines = [
            Text(
                f"{clock(e['ts'])}  {slug_of(e['actor'])}  {e['event']}  "
                f"{e.get('subject') or ''}  {safe(json.dumps(e.get('detail') or {}))}"
            )
            for e in found["events"]
        ]
        self.push_screen(TextScreen("full log", lines))


def run(cfg: Config) -> None:
    CoveyTop(cfg).run()
