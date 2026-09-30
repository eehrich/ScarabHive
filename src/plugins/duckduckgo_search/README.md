# duckduckgo_search

Web search without an account or key, through the `ddgs` package. The answer is a list of hits -- title, address,
short snippet -- for finding pages, not for reading them. Results are kept for a while, so the same query twice
costs one search.

- **Tool** `duckduckgo_search_web_search` -- `query`, `max_results` (1 to 20, default 10), `ignore_cache`,
  `cache_ttl`. Nothing found answers an empty list without an error; a failed search answers an error.
- No hooks, no panel.

Enable it in `config/plugins.yaml` (`duckduckgo_search: {type: duckduckgo_search, enabled: true}`) and allow
`+duckduckgo_search/*` in an agent's tool list. It needs `ddgs>=9.16`; do not install the old `duckduckgo-search`
next to it.

The full manual -- where the hits really come from, every parameter and answer field, the errors, retries, the
cache and the size of the answers -- is the plugin's guide, `duckduckgo_search.guide`, in the Help panel.
