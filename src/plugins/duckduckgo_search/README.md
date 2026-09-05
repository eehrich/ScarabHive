# duckduckgo_search

Web search without an account. One tool, `<instance>_web_search`, returns
titles, URLs and snippets from DuckDuckGo through the `ddgs` package.

## Configure

```yaml
# config/plugins.yaml
duckduckgo_search:
  type: duckduckgo_search
  enabled: true
  cache_ttl: 900        # seconds a result is reused for the same query
  cache_enabled: true
```

Give an agent the tool with `"duckduckgo_search/*"` in its allowlist.

## What to expect

- Snippets are short. The tool is for finding pages; reading one is the
  scraper's job (`web_scraper_page`).
- Rate limits, timeouts and engine failures are retried three times with
  backoff. A persistent failure comes back as `error` in the result, never as
  a raise.
- A search that genuinely found nothing returns `results: []` without an
  `error`, so "no hits for this query" is distinguishable from "the search
  failed".
- An empty answer is not cached, so the next call asks again.
- Results are cached per query and result count, case-insensitively.
  `ignore_cache: true` asks again.

## Install

The plugin needs `ddgs>=9.16` (in `requirements/all.txt`). Do not install the
older `duckduckgo-search` package next to it: it is the same project under its
previous name and returns zero results against today's DuckDuckGo.

## Quick check

```
python -m plugins.duckduckgo_search --query "godot texture filter"
```
