# User Management

The accounts of the server's sign-in -- username, e-mail, password, role, active state -- kept by an admin. Only an
active admin (checked against the database, not the token) may list or change them, and no admin can demote,
deactivate or delete themselves, so one active admin always remains. Web-only: no tools, no hooks.

- **Panel** Users -- figures, search, the accounts with role, state and last sign-in; create, edit, change a password,
  deactivate, activate, delete.
- **HTTP API** under `/plugins/user_management/` -- `GET/POST /users`, `PUT/DELETE /users/{id}`; the panel uses exactly
  these.

It needs `auth.enabled: true` in `config/config.yaml` and a first admin from the command line
(`agent-cli users create <name> <email> --admin`). The plugin is on in `config/plugins.yaml`
(`user_management: {type: user_management, enabled: true}`); the route rule for `/plugins/user_management/*` in
`config/security.yaml` keeps the panel for admins.

The full manual -- the panel, the account rules, who may do what, the API with its refusals, and the setup -- is the
plugin's guide, `user_management.guide`, in the Help panel.
