# setup

What an installation still lacks, and whether the chat answers. The first part
of the setup described in `docs/einrichtung_konzept.md`: keys are still
entered in `config/secrets.env`; the panel takes them once the loader reads
`config/local/`.

## Panel „Setup“ (admin)

- **Chat** — the default agent and its first LLM profile, as the chat started
  with them (a reload moves neither the entry agent nor its client); *Test the
  chat* sends one short request the way the agent would and shows the answer's
  arrival or the provider's error (a refused key reads as such).
- **Access** — whether an active admin still opens with a publicly known
  password (the configured default, and `admin`/`admin123`, which `config.yaml`
  ships: the admin is created once, so a default changed later is not its
  password). The logged-in admin concerned can change it right there, through
  `PATCH /auth/me`, which asks for the current one; logins made before stay
  valid until they expire. Asked of the configured user database; without
  authentication it is not asked. And whether tokens are signed with a known
  key — the model's default, an empty one, or one `config.yaml` has shipped,
  known for good to whoever has the repository's history — taken from the key
  the API set at start, which it signs with as long as it runs. A key reloaded
  into the config since (`/admin/reload-config`) reads as *needs a restart*, and
  a known one there needs fixing as well; an edit not reloaded is not seen.
  Without authentication the configured key is judged: it counts once
  authentication is on.
- **API keys** — every `${VAR}` the loaded config files name, as *set*,
  *missing* or *placeholder* (a copied template value such as `sk-or-v1-...`,
  which the loader cannot tell from a key and the provider refuses), with the
  sections that name it. Never a value.

`/plugins/setup/*` is admin only by its rule in `config.yaml`, which also keeps
the panel out of other users' launcher; `/state` and `/probe` check for an admin
themselves (`require_admin_viewer`: without authentication the one user is the
owner). `/probe` takes JSON only, so a page elsewhere cannot send one on the
admin's cookie.

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
tool's description tells the model never to ask for a key in the chat: the user puts it into
`config/secrets.env` and restarts; the panel shows its state.
A `null` in the status's `auth` means "cannot be told from here", never "no"
(`chat.profile` null: the default agent names none).
`setup_probe_chat` answers `ok: true`, or `ok: false` with the provider's error
cut to 300 characters; an unknown profile is answered with the profiles it can
probe (not the batch ones). Refused, both say why: "only an administrator may
…", or that the user database could not be read (locked, or not one) -- try
again, and if it persists the file needs checking. Nobody is let in then.

**Token and cache effect.** None on the prompt: no hook, nothing injected. A
probe costs one short request on the probed profile.

**Known gaps.**
- A key entered in `config/secrets.env` needs a restart: the loader reads the
  file once per process (`docs/einrichtung_konzept.md`, step 1 of the build).
- Which key the API signs with only the panel can tell: it runs in the API.
  `setup_status` may run elsewhere (agent-cli, a woken session), so it answers
  `null` for a restart pending, and for an own key configured -- a known one
  configured is `true` wherever it is read. A CLI run reads the configured user
  database without setting it up (opened read-only), and answers `null` for the
  admin's password where there is none yet (only `cli_user` gets that far then);
  one that cannot be read lets nobody in, `cli_user` included.
- A session the API wakes runs in an agent-cli process that loads the
  configuration from disk: whether authentication is on is judged there, not
  by the running API. Turned off in `config.yaml` without a restart, the woken
  session of any user is the owner while the API still checks logins. The
  same holds for every plugin in that process; which configuration a woken run
  follows is the core's to settle (handed over, `tmp/comm.txt` 28.09.2026).
