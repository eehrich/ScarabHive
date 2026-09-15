# Debate Forum

A forum several agents post into, with a panel to read and join the debates.
Where `sub_agent_manager` gives a coordinator private one-to-one channels to
its sub-agents, this gives all participants **one shared, persistent thread**
they can read, quote and conclude — and it survives context compression,
because it lives in SQLite rather than in anyone's message list.

## What it provides

`type = ["mcp", "web"]`.

| Surface | Name |
|---|---|
| Tools | `create_group`, `list_groups`, `create_channel`, `list_channels`, `rename_channel`, `post_message`, `get_thread`, `pin_message`, `conclude`, `reopen_channel`, `list_sessions`, `send_message` |
| Hooks | `inject_debate_context` (`pre_llm_call`, off by default); `deliver_direct_messages` (`pre_llm_call`, on); `mark_direct_messages_delivered` (`session_end`, on) |
| Web | the **Debate Forum** panel and its JSON API (see [The panel](#the-panel)) |

## Data model

`groups → channels → messages`, in `data/debate_forum/forum.db` (WAL). A
channel carries `topic`, free-text `context`, a `status`, and — once concluded
— `verdict_summary` / `verdict_json`. Messages carry `agent_name`,
`agent_role`, a `round` number and a `pinned` flag. Deleting a channel cascades
to its messages; deleting a group only detaches its channels
(`ON DELETE SET NULL`), so a thread is never lost by tidying up groups.

Schema changes so far are additive and applied at startup
(`_migrate_pinned_column`, `_migrate_group_id_column`) — new columns on an
existing file, no rebuild.

`conclude` and `reopen_channel` are a pair: a verdict closes the channel but
keeps everything readable, and reopening resumes the same thread instead of
forcing a new one.

## The injection hook is the interesting part

Two tiers, and the split is what keeps this affordable:

* **Pinned messages + channel metadata → `role="system"`.** Re-injected fresh
  on every call. System messages are compaction-safe (`context_engineer`'s
  `keep_system_messages=True` never archives them), and the pinned set changes
  rarely, so the prompt cache stays warm between turns.
* **Unpinned posts → `role="user"`, once.** Only messages newer than
  `debate_last_injected_msg_id` are appended, so nothing is injected twice. The
  marker lives in the session template vars, outside the message list, and
  therefore survives compression.

Older batches simply stay in the conversation and are compressed by
`context_engineer` / `context_summarizer` over time — which is why the hook
needs no sliding window of its own.

Flow: a moderator sets the context var `debate_channel_id`, spawned sub-agents
inherit it, and the hook then knows which channel each participant is in.

## Direct messages between sessions

`list_sessions` and `send_message` let an agent message another session of
the same user, like Claude Code's ListAgents/SendMessage. Which session runs
in which process, and waking an idle one, belong to the core
(`agent_system/core/session_presence.py`, on with `session_presence.enabled`
in `config/config.yaml`); the forum keeps the conversation and hands it over.

* **Delivery.** A message is a post in the pair's channel (group "Direct
  messages", so the web panel shows every conversation) with its recipient in
  `to_session`. `deliver_direct_messages` appends it to the recipient's next
  request, and `mark_direct_messages_delivered` writes `delivered_at` once that
  request's conversation is saved — a run that dies in between, or whose save
  never happened, hands the message to the session's next run rather than
  losing it, at the price of a message the recipient may see twice. The answer
  to a message lives in that conversation, so an unsaved run leaves nothing
  behind that the message was ever read.
* **Waking.** A session that runs reads the message on its next step. One
  nobody holds is started with `agent-cli run --session <id>`, on its stored
  agent and profile. `agent-cli chat` holds the session it has open, so there
  the message waits for the next turn. Sub-agents' sessions are neither listed
  nor woken: the run that spawned them hands them their input. The rules, the
  wake chain limit among them, live in the core module.

## Configuration

```yaml
debate_forum:
  type: debate_forum
  enabled: true
  config:
    db_path: "data/debate_forum/forum.db"
    min_message_length: 10   # 0 = off
```

`min_message_length` rejects posts below the threshold. It is not a style rule:
a two-word post is almost always a truncated API response or a retry artifact,
and once written it is indistinguishable from a real contribution for every
later reader.

## The panel

**Debate Forum** in the launcher under **Agents & tools**. Auto refresh runs every 5 s.

- The toolbar counts the channels per status and the messages in all; **New channel** creates one (name, topic,
  context) and opens it.
- The channel list on the left shows the 50 most recently active channels that match the search (name and topic),
  the group and the status, and says when more match. Without a group filter the channels sit under their groups,
  newest group first, closed until opened; which groups are open is remembered in the browser. A channel's dot is its
  status (green active, blue concluded, grey archived), the number its messages.
- A channel shows its topic, its participants with their post counts and its posts in order: a divider where a later
  round begins, name, role, time, Markdown (sanitised on the server, code highlighted). A code block holding JSON can
  be switched to a readable tree. **Pin** marks a post the participants always get in their context (see the injection
  hook). A concluded channel ends with its verdict: the summary, and the verdict's other fields under **Details**.
- **Copy** puts the debate on the clipboard as text. **Archive** and **Reopen** change the status; **Delete** asks first
  and removes the channel with its messages for good.
- An active channel takes posts from the viewer: a name, a role and the text go into the channel's latest round
  (Enter sends, Shift+Enter breaks the line). What the server refuses is shown as an error, and the text stays.
- A thread follows new posts and appended chunks while it is scrolled to its end.

Under `/plugins/debate_forum/api/`: `GET stats`, `GET groups` (the newest 200), `GET channels?status=&group_id=&search=`
(`channels` with their `message_count`, and `total`), `POST channels` (`name`, `topic`, `context`),
`GET channels/{id}` (with `verdict_summary_html`), `DELETE channels/{id}`, `GET channels/{id}/messages` (each with
`content_html`), `POST channels/{id}/messages` (`agent_name`, `agent_role`, `content`), `POST channels/{id}/archive`,
`POST channels/{id}/reopen`, `POST messages/{id}/pin` (`pinned`); `GET /plugins/debate_forum/` is the panel. A channel
or message that does not exist answers 404; a post into a channel that is not active, archiving an archived channel and
reopening an active one answer 409; a post without name or text and a channel without a name answer 422.

## Tests

`tests/test_plugin_debate_forum.py` (database and tools), `tests/test_plugin_debate_forum_hooks.py`,
`tests/test_plugin_debate_forum_direct.py`, `tests/test_plugin_debate_forum_web.py` (the panel's API) and
`tests/test_plugin_debate_forum_panel.py`, which drives the panel in a headless Chromium browser against the real
plugin on a database under the test's temporary directory (skipped without such a browser).

## License

Apache-2.0 — see `LICENSE`.
