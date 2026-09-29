# setup

What an installation still lacks, and whether the chat answers. Steps 0 and 1 of
the setup described in `docs/einrichtung_konzept.md`: the panel writes keys and
the signing key into this machine's own layer (`config/local.env`,
`config/local.yaml`, both never in the repository), through
`agent_system.config.local_layer`, which the install scripts use too.

## Panel „Setup“ (admin)

- **Chat** — the default agent and its first LLM profile, as the chat started
  with them (a reload does not move the entry agent); *Test the chat* sends one
  short request the way the chat's next message would -- its client built from
  the config as the API runs now, so a key saved here since is the one tried --
  and shows the answer's arrival or the provider's error (a refused key reads as
  such).
- **Access** — whether an active admin still opens with a publicly known
  password (the configured default, and `admin`/`admin123`, which `config.yaml`
  ships: the admin is created once, so a default changed later is not its
  password). The logged-in admin concerned can change it right there, through
  `PATCH /auth/me`, which asks for the current one. Asked of the configured
  user database; without authentication it is not asked. And whether tokens
  are signed with a key the repository has printed (`config.yaml`, the
  examples in the docs and reviews, the tests) — known for good to whoever has
  its history — taken from the key the API set at start, which it signs with
  as long as it runs, whatever a reload does. An empty key and the model's
  default count as known too; the API refuses to start with either. Where the
  config file names another key now, it reads as *needs a restart*; where that
  one is known, as a key to replace *before the next restart* — an own key
  running and a shipped one brought back by a pull, where the restart is the
  harm (or does not start at all). The file's key is expanded as a process
  started now would: `config/secrets.env` as it reads now, the real
  environment winning (`settings.environment_at_restart`). Without
  authentication the configured key is judged: it counts once authentication
  is on.
- **API keys** — every `${VAR}` the loaded config files name, and every
  `*_env` entry naming a variable its plugin reads itself (forge's
  `token_env`), as *set*,
  *missing* or *placeholder* (a copied template value such as `sk-or-v1-...`,
  which the loader cannot tell from a key and the provider refuses), with the
  sections that name it. Never a value. A key entered here goes into
  `config/local.env`, which a start reads before `config/secrets.env`, and into
  the API's environment; the config is then reloaded, so the chat's next
  message builds its client with it (the web chat always names its model), and
  so does a session the API wakes. Until a restart, the old key stays with
  every client built at start: an agent's default client (a `/run` that names
  no model -- writer jobs, stategraph), its fallbacks and escalation, the
  sub-agents the chat starts, the LLM clients of plugins (context_summarizer,
  lessons_learned), and the key a plugin read in its constructor (tavily_search;
  reload_plugin_configs lists it as unsupported). Replacing a running agent's
  `system_config` would move its auth and presence as well. A key set by the
  real environment cannot be entered: the environment wins at every start. A
  name only the auth section names is no API key and is not listed: the
  signing key has its own button, and a short one entered as a key would stop
  the next start.
- **Signing key** -- *Make an own key* writes a random `AUTH_SECRET_KEY` into
  `config/local.env` and points `auth.secret_key` at it in
  `config/local.yaml`; a restart applies it, and every login then ends.

`/plugins/setup/*` is admin only by its rule in `config/security.yaml`, which also keeps
the panel out of other users' launcher; `/state`, `/probe`, `/key` and `/signing-key` check for an admin
themselves (`require_admin_viewer`: without authentication the one user is the
owner). `/probe`, `/key` and `/signing-key` take JSON only, so a page elsewhere
cannot send one on the admin's cookie.

## Tools

| Tool | Does |
|---|---|
| `setup_status` | the panel's state as JSON: keys with state and the sections naming them, default agent and profile, admin password and signing key |
| `setup_probe_chat` | one short request through a profile (default: the default agent's first), with the configuration the chat started with; a batch profile is refused rather than queued |

Both answer only an active admin of the configured user database -- or the
owner: anyone without authentication, and agent-cli at the machine (`cli_user`,
where the configured user database is missing or holds no account of that name;
one that cannot be read lets nobody in). Not "a process without a user database": the
API wakes a web user's session in an agent-cli process under that user's name
(`session_presence.wake_command`). The key names come from the files the
configuration was loaded from, their state from the process environment. No
agent is allowed these tools yet; the setup agent comes with a later step.

## Model Experience

**What the model sees.** The two tool descriptions (`schema.yaml`). The status
says per key only `set`, `missing` or `placeholder` and the sections naming it —
where the variable is written, not every entry that inherits it through
`extends`; whether the chat answers, `setup_probe_chat` tells. The status
tool's description tells the model never to ask for a key in the chat: the user
enters it in the Setup panel.
A `null` in the status's `auth` means "cannot be told from here", never "no"
(`chat.profile` null: the default agent names none). `shared_signing_key` is the
key that signs where the tool runs in the API (a chat's tool call); elsewhere the
configured one, and an own one there reads `null`. `configured_signing_key_known`
is the one the config file names, which a restart applies.
`setup_probe_chat` answers `ok: true`, or `ok: false` with the provider's error
cut to 300 characters; an unknown profile is answered with the profiles it can
probe (not the batch ones). Refused, both say why: "only an administrator may
…", or that the user database could not be read (locked, or not one) -- try
again, and if it persists the file needs checking. Nobody is let in then.

**Token and cache effect.** None on the prompt: no hook, nothing injected. A
probe costs one short request on the probed profile.

**Known gaps.**
- A key written into a secrets file by hand needs a restart: the loader reads
  the files once per process. The panel's own write reaches the process's
  environment and the chat's next message, not what was built at start (see
  API keys above).
- Which key the API signs with only the API can tell: the panel, and
  `setup_status` called in a chat. Run elsewhere (agent-cli, a woken session)
  the tool answers `null` for a restart pending, and for an own key configured
  -- a known one configured is `true` wherever it is read. A CLI run reads the configured user
  database without setting it up (opened read-only), and answers `null` for the
  admin's password where there is none yet (only `cli_user` gets that far then);
  one that cannot be read lets nobody in, `cli_user` included.
- `cli_user` is a run without a user of its own (agent-cli without
  `--session-user`, `agent_run`): at the machine, but also the runs the writer
  starts for a book (`graph_audit`, `publish_pipeline`), whoever asked for it. None of their agents is allowed these tools; an agent
  that is must not be one such a run starts.
- A session the API wakes runs in an agent-cli process that loads the
  configuration from disk, with authentication on whenever the API that woke it
  enforces it (`HIVE_AUTH_REQUIRED`, 741006b6d). A wake from a process that
  does not (an API started without authentication, agent-cli at the machine)
  leaves the woken run to the disk: as open as its waker, or stricter.
