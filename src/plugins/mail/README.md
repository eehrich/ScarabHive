# Mail

Sends a plain-text mail over SMTP, for example to tell a person that a job has finished or failed. It writes only to
the recipients its server entry names: the model can pick some of them, never another address.

- **Tool** `mail_send` -- takes a subject, a plain-text body and optionally some of the configured recipients. It
  answers with the addresses the mail went to.

Enable it with a server entry that names the SMTP server and the recipients (`mail: {type: mail, enabled: true,
host: ..., recipients: [...]}`, the password as `${MAIL_PASSWORD}` from `config/secrets.env`) and allow `+mail/*` in
an agent's tool list.

The full manual is the plugin's guide, `mail.guide`, in the Help panel: the settings, the tool's parameters and
errors, and what the model sees.
