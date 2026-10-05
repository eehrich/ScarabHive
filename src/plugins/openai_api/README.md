# openai_api

The agents behind the OpenAI **Responses** and **Chat Completions** API: any OpenAI client (the `openai` SDK,
Open WebUI, LibreChat, n8n, IDE plugins) talks to an agent as a model named after it, with the agent's own prompt,
tools and sub-agents doing the work. A Responses conversation is an ordinary session of the calling user and shows up
in the web UI; Chat Completions calls leave nothing behind. Streaming and structured output are supported.

- **Endpoints** -- `GET /models`, `GET /models/{model}`, `POST /responses`, `POST /chat/completions` under
  `/plugins/<instance>/v1`; the key is the user's ScarabHive API key (or an access token).
- **Tools / hooks / panel** -- none.

Enabled in the shipped `config/plugins.yaml` (`openai_api: {type: openai_api, enabled: true}`); `agents`,
`blocked_agents` and `responses_db` go flat in that entry.

The full manual -- connecting a client, the endpoints, how a request becomes a turn, structured output, stored
conversations, errors and settings -- is the plugin's guide, `openai_api.guide`, in the Help panel.
