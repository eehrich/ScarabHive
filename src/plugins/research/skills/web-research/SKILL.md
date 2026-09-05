---
name: web-research
description: How to answer a question from the web so the answer holds up — choosing queries, ranking sources, reading rather than skimming snippets, cross-checking, and citing. Use for any task that needs facts, documentation or comparisons from the web.
metadata:
  version: '1.0.0'
---

# Web research

The failure mode to avoid is confident and wrong: an answer assembled from
search snippets, a stale forum post, or a page that says something slightly
different from what the snippet suggested. Every step below exists because
one of those happened.

## 1. Know what you are looking for

Before the first search, write one line for yourself: what fact, from what
kind of source, as of when. "The default texture filter in Godot 4.7, from
the official docs or source" is a question. "Godot texture filter" is not.

If the question is really several questions, list them and take them one at
a time. A comparison is at least two lookups plus a judgement.

## 2. Search like someone who knows the field

- Two or three queries beat one. Rephrase with the field's own vocabulary
  once you have seen a good page; the first query is usually a layman's.
- `site:` when you know who owns the fact: `site:docs.godotengine.org`,
  `site:github.com/<org>/<repo>`, `site:en.wikipedia.org`. The owner's page
  outranks any summary of it.
- Recent matters for versions, prices, APIs, releases: add the year or the
  version, or use the search tool's `time_range` when it has one.
- A search result is a lead. Its snippet is not evidence, and it is often
  cut mid-sentence. Read the page.

## 3. Rank sources before you read

From strongest to weakest, for a factual claim:

1. The source that owns the fact: official documentation, the specification,
   the repository, the paper, the vendor's own page, the primary article.
2. A well-maintained reference that cites the owner (a standards site, an
   encyclopedia with references, a maintained wiki).
3. A named expert's article or a high-signal Q&A answer with code and dates.
4. Blogs, forums, aggregators, SEO pages. Use only to find a stronger source,
   or when the question is about opinion and experience.

Open the top two to four, not the top ten. Prefer one strong source over five
weak ones. When two strong sources disagree, that is the finding.

## 4. Read, do not skim

- `web_scraper_page` returns boilerplate-stripped text with `total_chars` and
  `truncated`. For a long page, fetch the next window with `offset` instead of
  guessing from the first screen. The full text is cached, so paging is free.
- Look for the exact statement you will cite. Versions, defaults, numbers,
  dates: copy them as written, do not round or paraphrase them.
- A page dated years ago can still be right, but say so. A page that does not
  say which version it describes is weaker than one that does.
- A challenge page, a login wall, a cookie wall or a 403 comes back as an
  error. Do not retry the same URL; find the same content elsewhere (the
  owner's docs, a mirror, a cached copy, the repository).
- PDFs and other files: `web_scraper_download` saves them to the workspace
  when that tool is listed. Otherwise search for an HTML version.

## 5. Cross-check the surprising and the load-bearing

A claim that decides the answer, or that surprised you, gets a second source.
If the second source disagrees, report both with their URLs and say which you
find more credible and why. Do not average them into a claim nobody made.

## 6. Widen in parallel only when the question is wide

Forking sub-researchers costs a full prompt per fork and a round of
coordination. It pays when the question has **independent parts that each
need their own searching**: "compare A, B and C on price, licence and
maturity", "verify these four claims", "what do the Godot, Unity and Bevy docs
each say about X". It does not pay for one fact, one page, or parts that
depend on each other.

When you fork:
- One part per sub-researcher, phrased as a complete question with what
  counts as an answer and the date that matters. It has none of your context.
- Two to four forks. More is not wider, only slower to merge.
- Create all with `blocking=false`, then a single `wait_all`, then merge:
  their sources become your sources, attributed per part. Where two
  sub-answers disagree, that is a finding to report, not to smooth over.

## 7. Know when to stop

Stop when the question is answered with sources you read, or when the next
search would only add more of the same. A step budget spent on the eleventh
source is a step not spent on writing a careful answer.

## 8. Write the answer

The language of the question is the language of the answer, whatever language
the sources were in and whatever language a sub-researcher replied in.

- Answer first, in sentences a busy reader can act on.
- Then `Sources:` — one line per source: the URL and, in a few words, what it
  supports. Only URLs you opened. Never a URL you constructed or remembered.
- Exact quotes for the load-bearing statements when wording matters.
- What you could not find, said plainly, with what you tried.
