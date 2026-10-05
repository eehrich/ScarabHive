# Setup

What an installation still lacks -- the API keys the configuration names, whether the admin still opens with a
publicly known password, whether logins are signed with a known key -- and one short request to see whether the chat
answers. Keys are entered in the panel and saved in this machine's own layer (`config/local.env`,
`config/local.yaml`, never in the repository), never through a tool.

- **Tools** `setup_status` (the state as JSON, never a key's value) and `setup_probe_chat` (one request through an
  LLM profile); administrators only.
- **Panel** Setup (Administration) -- the chat test, the admin password and signing key, and every key with its
  state and a field to enter it.

The server entry comes with the plugin (`agents/setup.yaml`); the panel is admin only by its rule in
`config/security.yaml`. No agent is allowed the tools by default: allow `+setup/*` in an agent's tool list.

The full manual -- the panel, both tools with their answers and errors, the endpoints and what the model sees -- is
the plugin's guide, `setup.guide`, in the Help panel.
