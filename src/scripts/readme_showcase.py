"""The README's picture of the shell: a stub app with example data, never real data, no LLM, no network.

Serves the real shell page (templates and static files as the app serves them) to a signed-in admin, "Ada", with a
handful of plausible sessions. One of them, a vendor comparison, is stored as a real run is stored: the request, the
agent's tool calls (a web search, a research sub-agent with its own run, a scraper) and a markdown answer. The
Context & Cost Usage panel is the real plugin -- its router, its database under ``folder`` -- seeded with that run's
calls, so the shell can dock it beside the chat (``/?panel=...``). Taken by src/scripts/guide_screenshots.py:

    .venv/Scripts/python.exe src/scripts/guide_screenshots.py readme
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent_system import __version__
from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import ChatPreferences, User, UserPreferences, UserRole
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.catalog import Panel, build_catalog, core_panels
from agent_system.ui.resources import STATIC_DIR, ui_templates
from agent_system.ui.routes import router

MAIN = "5f2c8a41"  # the session the picture opens
SUB = "9d17e3b0"  # its research sub-agent's session
RID = "req_7c41e9a2d305"  # the run's request id; its calls ran under RID_001.., the sub-agent's run under RID_002_sub_..
SUB_RUN = f"{RID}_002_sub_3e8f1a"

USER = {"username": "ada", "full_name": "Ada", "email": "ada@example.org", "role": "admin"}

ANSWER = """\
Here is how the three shortlisted vendors compare for the media archive. Terms are taken from each vendor's \
published SLA page; the monthly cost assumes **50 TB stored and 5 TB egress**.

| | Vendor A | Vendor B | Vendor C |
|---|---|---|---|
| Durability | 11 nines | 11 nines | 11 nines |
| Availability SLA | 99.9 % | 99.95 % | 99.9 % |
| Service credit below SLA | 10 % | 25 % | 10 % |
| Egress | $0.05 / GB | free up to 1x stored | $0.02 / GB |
| Support response (P1) | 4 h | 1 h | 8 h, business days |
| Est. monthly cost | $1,410 | $1,180 | $1,090 |

**Recommendation: Vendor B.** It is not the cheapest, but it is the only one whose egress stays free at our \
restore volume, and it pairs the highest availability with a one-hour P1 response -- the two terms that matter \
most when an editor needs a clip back during a live edit.

- **Vendor C** saves about $90 a month, but its support answers on business days only.
- **Vendor A** has no advantage over B on any of the terms above.

Next step: ask Vendor B to confirm the egress allowance in writing before the contract review."""


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _result(call_id: str, name: str, content: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(content)}


MESSAGES = {
    MAIN: [
        {"role": "user", "request_id": RID,
         "content": "Compare the storage SLAs of our three shortlisted vendors (A, B and C): durability, "
                    "availability, egress cost and support response. Which one should we pick for the media archive?"},
        {"role": "assistant", "content": "", "step": 1,
         "reasoning_content": "I need the current SLA terms of all three vendors. A web search finds the SLA pages; "
                              "a researcher can read the terms of A and B in parallel while I check C myself.",
         "tool_calls": [_call("call_1", "tavily_search_web_search",
                              {"query": "Vendor A Vendor B Vendor C object storage SLA availability egress",
                               "max_results": 5}),
                        _call("call_2", "sub_agent_manager_manage_sub_agent",
                              {"action": "create", "agent_type": "researcher",
                               "task": "Collect the published SLA terms of Vendor A and Vendor B: durability, "
                                       "availability, service credits, egress pricing, P1 support response."})],
         "tool_request_ids": {"call_1": f"{RID}_001", "call_2": f"{RID}_002"}},
        _result("call_1", "tavily_search_web_search", {"status": "success", "results": [
            {"title": "Vendor A -- Service Level Agreement", "url": "https://vendor-a.example/legal/sla"},
            {"title": "Vendor B -- Storage SLA and credits", "url": "https://vendor-b.example/sla"},
            {"title": "Vendor C -- Terms of service: availability", "url": "https://vendor-c.example/legal/sla"}]}),
        _result("call_2", "sub_agent_manager_manage_sub_agent",
                {"status": "success", "agent_type": "researcher",
                 "result": "Vendor A: 99.9 %, 10 % credit, egress $0.05/GB, P1 4 h. Vendor B: 99.95 %, 25 % credit, "
                           "egress free up to 1x stored, P1 1 h. Both 11 nines durability."}),
        {"role": "assistant", "content": "", "step": 2,
         "reasoning_content": "The researcher has A and B. Vendor C's SLA page is the last one missing.",
         "tool_calls": [_call("call_3", "web_scraper_page", {"url": "https://vendor-c.example/legal/sla"})],
         "tool_request_ids": {"call_3": f"{RID}_003"}},
        _result("call_3", "web_scraper_page",
                {"status": "success", "title": "Vendor C -- Terms of service: availability",
                 "content": "Monthly uptime 99.9 %. Service credit 10 %. Egress $0.02 per GB. Priority 1 "
                            "incidents: first response within 8 business hours."}),
        {"role": "assistant", "content": ANSWER, "step": 3,
         "reasoning_content": "All terms are in. Cost at 50 TB stored and 5 TB egress; B's egress allowance "
                              "covers our restore volume."},
    ],
    SUB: [
        {"role": "user", "request_id": SUB_RUN,
         "content": "Collect the published SLA terms of Vendor A and Vendor B: durability, availability, "
                    "service credits, egress pricing, P1 support response."},
        {"role": "assistant", "content": "", "step": 1,
         "tool_calls": [_call("call_1", "web_scraper_page", {"url": "https://vendor-a.example/legal/sla"}),
                        _call("call_2", "web_scraper_page", {"url": "https://vendor-b.example/sla"})],
         "tool_request_ids": {"call_1": f"{SUB_RUN}_001", "call_2": f"{SUB_RUN}_002"}},
        _result("call_1", "web_scraper_page", {"status": "success", "title": "Vendor A -- Service Level Agreement"}),
        _result("call_2", "web_scraper_page", {"status": "success", "title": "Vendor B -- Storage SLA and credits"}),
        {"role": "assistant", "step": 2,
         "content": "Vendor A: 99.9 %, 10 % credit, egress $0.05/GB, P1 4 h. Vendor B: 99.95 %, 25 % credit, "
                    "egress free up to 1x stored, P1 1 h. Both 11 nines durability."},
    ],
}


def _sessions(now: datetime) -> list[dict]:
    rows = [(MAIN, "Compare storage vendor SLAs", "assistant", 4, True),
            ("a83d0c57", "Refactor the upload client", "coder", 95, False),
            ("e41b9f26", "Weekly newsletter draft", "assistant", 60 * 5, False),
            ("0c6a2d98", "Blog post: prompt caching explained", "assistant", 60 * 26, False),
            ("7be52f13", "Nightly build review", "coder", 60 * 29, False),
            ("d29f4e70", "Podcast episode research", "researcher", 60 * 24 * 3, False)]
    return [{"session_id": sid, "title": title, "agent_name": agent, "depth": 0, "has_children": kids, "children": [],
             "updated_at": (now - timedelta(minutes=ago)).isoformat(),
             "created_at": (now - timedelta(minutes=ago + 3)).isoformat()}
            for sid, title, agent, ago, kids in rows]


def _seed_usage(tracker, monkeypatch, now: datetime) -> None:
    """The run's calls as the tracker records them: three of the assistant, two of its researcher."""
    import plugins.context_usage_tracker.tracker as tracker_module

    start = (now - timedelta(minutes=6)).timestamp()
    calls = [  # seconds after start, agent, session, request, prompt, cached, completion, cost, model, latency
        (0, "assistant", MAIN, RID, 6_240, 0, 412, 0.0214, "model-pro", 3_900),
        (9, "researcher", SUB, SUB_RUN, 3_180, 0, 236, 0.0011, "model-flash", 1_450),
        (21, "researcher", SUB, SUB_RUN, 9_460, 2_900, 305, 0.0023, "model-flash", 2_100),
        (38, "assistant", MAIN, RID, 14_870, 5_980, 188, 0.0296, "model-pro", 2_700),
        (52, "assistant", MAIN, RID, 21_350, 14_620, 1_462, 0.0381, "model-pro", 11_800),
    ]
    for at, agent, session_id, request_id, prompt, cached, completion, cost, model, latency in calls:
        monkeypatch.setattr(tracker_module, "time", SimpleNamespace(time=lambda at=at: start + at))
        tracker.record_usage(f"{agent}-1", agent, session_id, total_tokens=prompt + completion,
                             prompt_tokens=prompt, completion_tokens=completion, context_window=200_000,
                             cached_tokens=cached, cost=cost, model=model, request_id=request_id, latency_ms=latency)


def showcase_app(folder: Path, monkeypatch) -> FastAPI:
    from plugins.context_usage_tracker.plugin import ContextUsageTrackerPlugin

    now = datetime.now(timezone.utc)
    sessions = _sessions(now)
    sub_session = {"session_id": SUB, "title": "Collect SLA terms of Vendor A and B", "agent_name": "researcher",
                   "depth": 1, "has_children": False, "children": [], "runs": [SUB_RUN],
                   "created_at": (now - timedelta(minutes=6)).isoformat(),
                   "updated_at": (now - timedelta(minutes=5)).isoformat()}
    usage = ContextUsageTrackerPlugin("context_usage_tracker", {}, {"storage_path": str(folder / "usage.json")})
    _seed_usage(usage.tracker, monkeypatch, now)

    app = FastAPI()
    templates = ui_templates()
    preferences = UserPreferences(chat=ChatPreferences(fold_steps="never", sub_agents="collapsed")).model_dump()

    @app.get("/")
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/ui/catalog")  # before the real router: this one wins
    async def catalog():
        core = core_panels(audit_enabled=False, profiling_enabled=False, memory_profiling_enabled=False)
        panel = Panel("context_usage_tracker", "Context & Cost Usage", "/plugins/context_usage_tracker/",
                      "chart-column", "context", "Context, token and cost usage per agent and per call",
                      ["tokens", "cost", "usage"], window={"width": 900, "height": 760},
                      contexts={"session": "/plugins/context_usage_tracker/?session_id={session_id}"})
        return build_catalog("admin", core, [panel])

    @app.get("/auth/me")
    async def me():
        return USER

    @app.get("/auth/me/preferences")
    async def my_preferences():
        return preferences

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "uptime_seconds": 3600}

    @app.get("/chat/commands")
    async def chat_commands():
        return {"commands": [], "skills": [], "plugin_commands": []}

    @app.get("/agents")
    async def agents():
        details = [{"name": "assistant", "description": "General help with tools and sub-agents", "tags": ["chat"]},
                   {"name": "researcher", "description": "Searches the web and reads sources", "tags": ["research"]},
                   {"name": "coder", "description": "Reads, changes and tests code", "tags": ["code"]}]
        return {"agents": [d["name"] for d in details], "default": "assistant", "details": details}

    @app.get("/llm/profiles")
    async def profiles():
        return {"default": "balanced", "profiles": [
            {"name": "balanced", "description": "Good answers at a fair price", "model_ref": "model-pro",
             "max_steps": 30, "provider": "openai_httpx", "model": "model-pro", "host": None},
            {"name": "fast", "description": "Quick and cheap", "model_ref": "model-flash",
             "max_steps": 30, "provider": "openai_httpx", "model": "model-flash", "host": None}]}

    @app.get("/api/sessions/hierarchy")
    async def hierarchy():
        return {"sessions": sessions, "root_count": len(sessions)}

    @app.get("/api/sessions/active")
    async def active():
        return {"active": {}}

    @app.get("/api/sessions/{session_id}/children")
    async def children(session_id: str):
        return {"sessions": [sub_session] if session_id == MAIN else []}

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        found = next((s for s in [*sessions, sub_session] if s["session_id"] == session_id), None)
        if found is None:
            raise HTTPException(status_code=404)
        return {**found, "llm_profile": "balanced", "context_vars": {},
                "metadata": {"sub_agents": {SUB: {}}} if session_id == MAIN else {},
                "messages": MESSAGES.get(session_id, [
                    {"role": "user", "content": found["title"]},
                    {"role": "assistant", "content": "Done -- see the notes above.", "content_format": "text"}])}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("context_usage_tracker", usage)
    registry.apply_to_app(app)
    app.dependency_overrides[get_optional_user] = lambda: User(
        id=1, username="ada", email=USER["email"], role=UserRole.ADMIN, created_at=now)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
