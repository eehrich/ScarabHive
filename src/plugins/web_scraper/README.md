# web_scraper

Fetch web pages for an agent. Two tools:

| tool | does |
|---|---|
| `<instance>_page` | A URL as readable text (default) or as its list of links. Boilerplate stripped, paged with `offset`, tables and lists on request. |
| `<instance>_download` | Saves what a URL serves — PDF, image, archive, anything — to a file inside an allowed directory. Only offered when a directory is configured. |

## Configure

```yaml
# config/plugins.yaml
web_scraper:
  type: web_scraper
  enabled: true
  proxies: []                 # optional pool, rotated per domain
  cache_ttl: 1800             # seconds a fetched page is reused
  user_agent: ""              # empty = rotate browser identities; see below
  allowed_directories:        # where `download` may write; empty = no download tool
    - data/workspace
  max_download_mb: 100
```

Give an agent the tools with `"web_scraper/*"` in its allowlist.

## What to expect

- **Text is paged.** `page` returns `max_chars` (default 8000) of text plus
  `total_chars` and `truncated`. The full text is cached, so the next window
  with `offset` costs no second fetch. `max_chars: 0` returns everything.
- **Text is content, not furniture.** Scripts, styles, `nav`, `footer`,
  `aside`, iframes and SVGs are removed. Inline tags do not break sentences,
  block tags end lines. Every script survives — a Japanese page is a page.
- **Non-text URLs are refused with a pointer.** A PDF comes back as an error
  naming `<instance>_download`; the model then knows what to do.
- **Bot walls are errors, not page text.** 403, 429 and 503, a challenge
  title ("Just a moment…", "Attention Required", "Access Denied") or a known
  challenge marker (Cloudflare, DataDome, PerimeterX) end the call with
  `error: blocked: …`. Words in the body are never used for this: a page that
  loads one script from cdnjs.cloudflare.com is not a Cloudflare challenge.
- **Retries** on transport errors and 429/502/503/504, three times with
  backoff. A 404 is not retried.
- **Links** are filtered when asked, from the cached page: `include_nofollow`,
  `only_same_domain`, `max_links`.
- **Downloads** stream to a `.part` file and are renamed on success; a failed
  or oversized download leaves nothing behind. The result carries `bytes`,
  `content_type`, `sha256` and the final URL. Existing files are kept unless
  `overwrite: true`.

## Sites that want to know who you are

By default the scraper rotates through browser identities. Some sites refuse
those and want a descriptive one with a way to reach you. Wikipedia is the
common case: it answers a browser identity with `403` and a pointer to its
robot policy, and a descriptive one with normal traffic. Set one, with a
contact address that actually reaches you:

```yaml
user_agent: "MyProject-research/1.0 (https://example.org; me@example.org)"
```

It then applies to every request the instance makes, reading and downloading.

## Security

Every URL, and every redirect hop, is checked before it is fetched: http(s)
only, and the host must resolve to a public address. Loopback, RFC1918,
link-local (the cloud metadata IP included) and the metadata hostnames are
refused as `Blocked URL (SSRF protection)`. The model controls the URL, so
without this a prompt-injected page could make the scraper read an internal
service and hand the body back.

Cookie jars and the page cache are scoped per session, so one user's
authenticated page is never served to another.

## Quick check

```
python -m plugins.web_scraper --url https://example.com
python -m plugins.web_scraper --url https://example.com --links
```
