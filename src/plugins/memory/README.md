# Memory

Notes agents keep for later -- a fact, a decision, a preference -- with a title, a text, keywords and an importance.
Memories belong to the chat session they were stored in; an agent finds them by id or by meaning (semantic search
over local embeddings). The **Memory** panel shows, searches and deletes the memories of a session.

- **Tool** `memory` -- one tool with the operations `store`, `recall`, `search`, `list`, `update` and `delete`.
- **Hook** `inject_memory_context` (off by default) -- appends the ids and titles of the relevant memories to the
  history before an LLM call, only when they changed, so the cached prompt stays intact.
- **Panel** Memory -- figures, the most recently used memories, search by meaning, delete.

Enable it in `config/plugins.yaml` (`memory: {type: memory, enabled: true}`) and allow `+memory/*` in an agent's
tool list. It needs a vector store: `chromadb` (or `sqlite-vec` with `sentence-transformers`).

The full manual -- the panel, every parameter and answer, the hook's settings and the server settings -- is the
plugin's guide, `memory.guide`, in the Help panel.
