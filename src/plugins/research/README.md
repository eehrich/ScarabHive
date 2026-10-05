# research

A web research agent: it searches, reads the pages that matter, cross-checks the claims that decide the
answer, and answers with the URLs it read. A question with independent parts it splits over up to four
parallel sub-researchers. Configuration only -- two agents, a prompt and the `web-research` skill; the
search and reading tools are the shared `tavily_search` (with a key), `duckduckgo_search` (without one)
and `web_scraper` instances.

- **Agents** `research_agent` (UI and tool, 40 steps) and `research_worker` (one part of a wide question,
  25 steps, cannot branch or ask the user).
- **Tools** -- none of its own; other agents call it as `research_agent_execute_task` (`"+research_agent/*"`
  in their allowlist) or spawn it through a sub-agent manager.
- **Hooks / panel** -- none of its own.

Nothing to enable: the agent YAMLs and the skill are picked up by convention. Use it with
`agent-cli chat --agent research_agent` or by picking it in the web UI.

The full manual -- the method, wide questions, calling it from other agents, what the model sees and the
settings -- is the plugin's guide, `research.guide`, in the Help panel.
