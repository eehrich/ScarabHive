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

## Wide questions

For a question with independent parts — comparing three libraries, checking
four claims — the agent starts one `research_worker` per part, waits for them
all, and merges the answers with their sources. A single question is faster
done directly, and the prompt says so.

A worker has the same tools for searching and reading but no sub-agent
manager, so a branch cannot branch again. That holds wherever the research
agent itself is running: started from the UI, or spawned as a sub-agent by
another coordinator.

## What lives here

| | |
|---|---|
| `agents/research_agent.yaml` | `type: basic_agent`, chain `or-deepseek-flash` → `deepseek-chat`, 40 steps, visibility `both` |
| `agents/research_worker.yaml` | The branch for a wide question: same prompt, same skill, same reading tools, 25 steps, and no sub-agent manager |
| `agents/prompts/research_agent.md` | Role, tool hints, output format. Short on purpose. Both agents render it; the branching section appears only for the one that can branch. |
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
