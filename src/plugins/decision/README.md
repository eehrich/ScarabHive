# Decision

Lets an agent ask a decision model -- TypeSafe's Jev, or a local Laya, whichever the decision profile names --
yes/no and rating questions about many items at once. The model writes no text: per item and question it answers
with a number, a probability or a position on an ordered scale (counted from 0). One request per item, ten at a
time, up to 250 items and 20 questions per call; the answer is a compact table plus what the batch cost.

- **Tools** `decision_evaluate_probabilities` (yes/no questions, 0 to 1) and `decision_evaluate_scores` (ordered
  scales, lowest first).
- **Hooks / panel** -- none.

Enabled in `config/plugins.yaml` (`decision: {type: decision, enabled: true}`); allow `+decision/*` in an agent's
tool list. The model comes from `llm_system.default_decision_profile` unless the entry names a `decision_profile`.

The full manual -- the parameters, the answer and every error, the server settings and what the model sees -- is
the plugin's guide, `decision.guide`, in the Help panel.
