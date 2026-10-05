# Twitter search

Searches posts on X (formerly Twitter) from the last seven days through the official X API v2 (via `tweepy`):
up to 50 posts per call with author, date, language and engagement counts. It needs an X developer app's bearer
token, and X bills every post read; nothing is cached. It has no hooks and no panel.

- **Tool** `twitter_search_tweets` -- a query in X search syntax and an optional `limit` (1 to 50, default 10).

Enable it in `config/plugins.yaml` (`twitter_search: {type: twitter_search, enabled: true}`), put
`TWITTER_BEARER_TOKEN` in `config/secrets.env`, restart, and allow `+twitter_search/*` in an agent's tool list.

The full manual -- the parameters, the answer, every error and the rate limits, what a call costs, and running
the plugin on its own -- is the plugin's guide, `twitter_search.guide`, in the Help panel.
