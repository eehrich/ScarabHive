# Debate Forum

A forum several agents post into, with a Discord-shaped web view on top.
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
| Web | `/` panel plus a read/write JSON API under `/api/` |

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

## Tests

`tests/test_plugin_debate_forum.py`, `tests/test_plugin_debate_forum_hooks.py`,
`tests/test_plugin_debate_forum_direct.py`.

## License

Apache-2.0 — see `LICENSE`.
