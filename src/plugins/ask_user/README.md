# ask_user

The model asks the person watching the run a question and waits for the
answer: which reading of the request is meant, which option they prefer,
whether to take a step that is hard to undo. The question appears in the web
chat under the run, with up to four options and a field for an answer in the
person's own words -- or in `agent-cli chat`, numbered, answered by the next
line typed. A run nobody watches -- the API, agent-run, a one-shot agent-cli,
a job, a closed tab -- is never asked: the tool answers at once, and the model
decides itself and says what it assumed.

- **Tool:** `ask_user` (named like the instance) -- a question, optionally
  2 to 4 options and `multi_select`; the answer is the result.
- **Answer UI:** a box on the call's row in the web chat; the answer goes to
  `POST /plugins/ask_user/answer` (the run's owner or an admin, never an API
  key). In `agent-cli chat` the question is printed, its options numbered,
  and the next line typed answers it in the process. No hooks, no panel.

Enable it in `config/plugins.yaml` (`type: ask_user`; the default
configuration loads it) and give an agent the tool with `+ask_user/*` in its
allowlist.

The full manual -- answering a question, who is asked, waiting, sub-agents,
what the model sees, settings -- is `ask_user.guide` in the Help panel.
