# Tavily search

Web search for agents through the paid [Tavily API](https://tavily.com/): ranked results with excerpts, on request
page text and a short answer written from the results, plus the readable content of up to 20 pages at once (each
page cut to `max_content_chars`, 20000 by default).
Results are cached on disk for half an hour. Without an API key the plugin offers no tools; agents then search
with `duckduckgo_search`. It has no panel.

- **Tool** `tavily_search_web_search` -- a query with optional depth, topic, time range and domain filters.
- **Tool** `tavily_search_extract` -- the content of 1 to 20 URLs, as markdown or text.

Enable it in `config/plugins.yaml` (`tavily_search: {type: tavily_search, enabled: true, api_key: "${TAVILY_API_KEY}"}`),
put `TAVILY_API_KEY` in `config/secrets.env`, and allow `+tavily_search/*` in an agent's tool list.

The full manual -- every parameter, answer and error, the cache, what a call costs, how large an answer can get,
and the server settings -- is the plugin's guide, `tavily_search.guide`, in the Help panel.
