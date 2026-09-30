# HTTP Server

Puts one plugin behind a small REST interface, so that a program outside ScarabHive -- a script, a cron job,
another service -- can call that plugin's tools over HTTP. Started from the command line
(`python -m plugins.http_server --server-name <plugin>`), it serves `GET /health` and `POST /call` on
`127.0.0.1:9000` until it is stopped. Without an API key it binds only a loopback address and answers only
loopback Host names; with `HTTP_SERVER_AUTH_KEY` set, `/call` needs the key.

- **Command line** -- `--server-name` (an instance from `config/plugins.yaml` or a plugin type), `--host`,
  `--port`, `--config`, `--no-ssl-verify`.
- No agent tools, no hooks, no panel: enabling it in `config/plugins.yaml` gives agents nothing.

Whoever can reach `/call` can run every tool of the served plugin; no agent allowlist applies.

The full manual -- starting it, the HTTP interface and its answers, who can call it, what the model sees and the
settings -- is the plugin's guide, `http_server.guide`, in the Help panel.
