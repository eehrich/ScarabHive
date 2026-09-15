# User Management

The **Users** panel: the accounts of the auth database (`data/users.db`, see
`src/agent_system/auth/`), administered by an admin. Web-only plugin, no MCP tools.

## Requirements

- `auth.enabled: true` in `config/config.yaml`. With authentication off the panel says so, and the API refuses.
- An active admin account. Create the first one on the command line:

  ```bash
  agent-cli users create admin admin@example.com --admin
  ```

## The panel

Opened from the panel launcher (category *admin*) or at `/plugins/user_management/`.

- **Stats**: users, active users, active admins.
- **Search**: filters the table by username, email and full name.
- **Table**: username with full name, email, role, state, created, last login. `(you)` marks your own account, a key
  marks an account with an API key.
- **New user**: username, email, full name, role, active, password. A refusal (name or email taken, password too
  long, email invalid) is shown in the dialog.
- **Edit**: email, full name, role, active, and a new password (left empty, the password stays). Only what was changed
  is sent, so a change made meanwhile by someone else is not overwritten.
- **Deactivate / Activate**: deactivating asks first; an inactive account can no longer sign in.
- **Delete**: asks first.

Your own account cannot be deactivated, deleted or given another role here — the buttons are off and the server
refuses. Another admin does that.

## Endpoints

All under `/plugins/user_management/`, JSON in and out.

| Method | Path | Does |
|---|---|---|
| `GET` | `/` | the panel |
| `GET` | `/users` | `{"me": <your id>, "users": [...]}` — every account with `id, username, email, full_name, role, is_active, created_at, last_login, has_api_key` |
| `POST` | `/users` | create: `username, email, password`, optional `full_name, role, is_active` |
| `PUT` | `/users/{id}` | change any of `email, full_name, role, is_active, password`; only the fields sent |
| `DELETE` | `/users/{id}` | delete |

Answers: `401` not signed in (or a refresh token), `403` not an active admin or authentication off, `404` no such
account, `409` name or email taken, or a change to your own role, active state or existence, `422` invalid input:
role not one of `admin`, `user`, `guest`; email invalid; username not 3–50 of letters, digits, `_`, `-`; password
shorter than 8 characters or longer than 72 bytes. `415` a creation sent without `Content-Type: application/json`.

## Permissions

Two layers:

1. **Route rules** in `config/config.yaml`: the `auth.plugin_security.endpoint_rules` entry for
   `/plugins/user_management/*` requires the admin role. The same rules decide who sees the panel in the launcher.
2. **The plugin itself** checks every data endpoint, whatever the rules say: a signed-in account that is **active** and
   an **admin in the database** — not the role written in the token. Tokens only (cookie or `Authorization: Bearer`);
   an API key is refused.

Credentials never leave the server: no password hash, no API key, only `has_api_key`.

Since the requesting admin is checked right before each write and cannot change their own role, active state or
account, one active admin always remains — within one API process. Two processes writing the same database at the same
moment are not serialized.

## Tests

```bash
pytest src/plugins/user_management/tests -q
```

- `test_plugin_user_management.py` — the API against a users database under `tmp_path`: who is refused, lock-out,
  validation, no credentials in answers.
- `test_plugin_user_management_panel.py` with `panel_tests.html` — the panel in a headless Chromium against the real
  plugin behind the app's route security, including a non-admin whose every change the server refuses, and the
  instance with authentication off. Skipped without a Chromium-based browser.
