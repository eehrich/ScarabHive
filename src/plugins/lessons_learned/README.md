# Lessons Learned

What agents learn and should keep doing, kept across sessions per agent: a title, a text, a category, a priority and a
confidence that confirming and contradicting evidence moves. Agents store lessons, teach them to other agents and
confirm them; similar lessons are recognised by meaning (semantic search over local embeddings) and become evidence
instead of a second copy. The **Lessons Learned** panel reviews, activates, edits, merges and cleans up the lessons
of every agent.

- **Tool** `lessons_learned` -- one tool with the operations `store`, `search`, `list`, `update`, `confirm`, `teach`
  and `delete`.
- **Hooks** (off by default) -- `inject_lessons` appends an agent's active lessons to the history before an LLM call,
  only when they changed, so the cached prompt stays intact; `extract_lessons` lets an LLM pick lessons out of the
  conversation after each request, reading on from the last message it read.
- **Panel** Lessons Learned -- figures, filters, search by meaning, the editor, LLM consolidation of similar lessons,
  cleanup by filter.

Enable it in `config/plugins.yaml` (`lessons_learned: {type: lessons_learned, enabled: true}`) and allow
`+lessons_learned/*` in an agent's tool list. Search and the duplicate check need a vector store: `chromadb` (or
`sqlite-vec`).

The full manual -- the panel, every parameter and answer, how confidence and duplicates work, the hooks' settings and
the server settings -- is the plugin's guide, `lessons_learned.guide`, in the Help panel.
