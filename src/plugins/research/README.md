# research plugin

A web research agent: it searches, reads the pages that matter, cross-checks,
and answers with URLs. Config only, no tools of its own.

## Use it

```
agent-cli chat --agent research_agent
```

Other agents get it in two ways. As a **tool**, the agent exposes
`research_agent_execute_task`; put `"research_agent/*"` in the caller's
allowlist. As a **sub-agent**, add `research_agent` to a sub-agent manager's
`allowed_agents` (the shared manager in `config/plugins.yaml` and the
sysadmin's already have it).

## With or without a Tavily key

| | search | read a page | read many at once |
|---|---|---|---|
| `TAVILY_API_KEY` set | `tavily_search_web_search` | `web_scraper_page` | `tavily_search_extract` |
| no key | `duckduckgo_search_web_search` | `web_scraper_page` | one at a time |

The switch is not in the prompt. Without a key the `tavily_search` server
renders no tools, so the agent only ever sees tools that work. DuckDuckGo
needs no account; it returns titles and snippets, so the scraper does the
reading.

`web_scraper_download` saves PDFs and other files into `data/workspace`,
where the coder, gamedev and amiga harnesses can read them.

## What lives here

| | |
|---|---|
| `agents/research_agent.yaml` | `type: basic_agent`, chain `or-deepseek-flash` → `deepseek-chat`, 40 steps, visibility `both` |
| `agents/prompts/research_agent.md` | Role, tool hints, output format. Short on purpose. |
| `skills/web-research/SKILL.md` | The method: query design, source ranking, reading with `offset`, cross-checking, citing. Always in the prompt. |
| `tests/test_research_config.py` | The prompt is rendered and read back; the tool instances it names exist; it is registered where it is meant to be spawned. |

The knowledge is in the skill, not the prompt, so it can be read, corrected
and reused without touching the agent.

## Tuning

| want | change |
|---|---|
| a different model | `llm_profile` in `agents/research_agent.yaml`; keep two routes so one provider's outage does not stop the chain |
| deeper research per question | `max_steps` (40 covers search, three pages, one long page paged, a cross-check) |
| a different method | `skills/web-research/SKILL.md`; the agent reads it on every run |
| downloads elsewhere | `allowed_directories` on the `web_scraper` instance in `config/plugins.yaml` |
