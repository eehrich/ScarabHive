# Lessons Learned Plugin

Persistent lesson storage with confidence tracking, semantic search, and automatic extraction for AgentSystem.

## Overview

The Lessons Learned plugin enables agents to learn from experience across sessions. It captures insights, mistakes, preferences, and best practices, then automatically injects relevant lessons into future conversations. Lessons are tracked with dynamic confidence scores that update based on evidence (confirms/contradicts) and effectiveness metrics.

## Features

- **Persistent Learning**: Lessons survive across sessions and agent restarts
- **Confidence System**: Dynamic confidence scores adjust based on evidence and effectiveness
- **Evidence Tracking**: Track confirms, contradicts, and neutral evidence for each lesson
- **Review History**: Full audit log of all reviews with structured analytics
- **Semantic Search**: ChromaDB-powered vector search finds relevant lessons by meaning
- **Automatic Extraction**: LLM analyzes conversations at session end to extract new lessons
- **Deduplication**: Automatic detection and merging of duplicate lessons (similarity-based)
- **Prompt Injection Hook**: appends relevant active lessons as a `developer` turn, only when the set changed
- **Cross-Agent Teaching**: Share lessons between agents with lineage tracking
- **Panel**: browse, search, edit, consolidate and clean up lessons (see [The panel](#the-panel))
- **Categories**: Organize lessons by type (style, workflow, error_pattern, domain_knowledge, tool_usage, etc.)

## Architecture

### Storage

- **SQLite**: Structured data (lessons, evidence, applications, categories, extraction logs)
- **VectorStore**: ChromaDB embeddings for semantic search and deduplication
- **Database**: `data/lessons_learned/lessons.db` (configurable)

### Key Tables

| Table | Purpose |
|-------|---------|
| `lessons` | Core lesson data (title, content, confidence, status, metadata) |
| `lesson_evidence` | Evidence records (confirms, contradicts, neutral) |
| `lesson_applications` | Application tracking (when lesson was used, outcome) |
| `review_history` | Full audit log of all reviews with timestamps |
| `categories` | Custom categories per agent |
| `extraction_log` | Extraction attempt records (success/failure, token usage) |

### Confidence System

Confidence is dynamically calculated as:

```
confidence = base_confidence × evidence_factor × effectiveness_factor
```

**Base Confidence** (by source type):
- `manual` (the panel): 0.8
- `cross_agent` (taught by another agent): 0.6
- `reflection` (agent tool): 0.5
- `auto` (extracted at session end): 0.4

**Evidence Factor**:
- No evidence: 1.0 (neutral)
- With evidence: `0.5 + (confirms / total_evidence)` (scales 0.5→1.5)

**Effectiveness Factor**:
- No applications yet: 1.0
- With applications: `success_rate × 2.0` (scales 0.0→2.0)

**Auto-Status Changes**:
- Confidence drops below 0.2 → `active` → `inactive`
- Confidence rises above 0.4 → `inactive` → `active`

## Configuration

### Plugin Configuration (`config/plugins.yaml`)

```yaml
plugins:
  servers:
    lessons_learned:
      type: lessons_learned
      enabled: true
      description: "Persistent lesson learning and management"
      config:
        database_path: "data/lessons_learned/lessons.db"
        max_lessons_per_agent: 200
        dedup_similarity_threshold: 0.82
        exact_duplicate_threshold: 0.95
        llm_profile: "turbo"
```

### Hook Configuration

The plugin provides two hooks:

#### 1. Inject Lessons Hook (pre_llm_call)

Appends active lessons to the history before LLM calls. **Disabled by default** — enable per-agent:

```yaml
agents:
  my_agent:
    hooks:
      lessons_learned.inject_lessons:
        enabled: true
```

#### 2. Extract Lessons Hook (session_end)

Extracts lessons from conversation at session end using LLM analysis. **Disabled by default** — enable per-agent:

```yaml
agents:
  my_agent:
    hooks:
      lessons_learned.extract_lessons:
        enabled: true
```

## Tools

### `lessons_learned` Tool

Main tool for managing lessons with 6 operations.

#### Operation: `store`

Store a new lesson (manual or via agent tool).

```python
await agent.call_tool("lessons_learned", {
    "operation": "store",
    "title": "Always validate user input before processing",
    "content": "User reported errors when submitting empty forms. Always add input validation with clear error messages before processing data.",
    "category": "error_pattern",
    "priority": 8,
    "tags": ["validation", "user-input", "error-handling"]
})
```

**Response:**
```json
{
    "lesson_id": "les_001",
    "title": "Always validate user input before processing",
    "confidence": 0.5,
    "status": "draft",
    "created_at": "2024-01-29T14:30:22.123Z",
    "message": "Lesson stored successfully"
}
```

**Parameters:**
- `title` (required): Short descriptive title (max 200 chars)
- `content` (required): Detailed lesson content (max 2000 chars)
- `category` (optional): One of `style`, `workflow`, `error_pattern`, `domain_knowledge`, `tool_usage`, `communication`, `performance`, `quality`, `general` (default: `general`)
- `priority` (optional): 1-10 scale (default: 5)
- `tags` (optional): Array of strings for categorization

#### Operation: `search`

Semantic search for lessons by query string.

```python
await agent.call_tool("lessons_learned", {
    "operation": "search",
    "query": "input validation best practices",
    "limit": 5,
    "agent_name": "my_agent"  # Optional filter
})
```

**Response:**
```json
{
    "results": [
        {
            "lesson_id": "les_001",
            "title": "Always validate user input before processing",
            "content": "...",
            "confidence": 0.75,
            "similarity": 0.92,
            "status": "active",
            "evidence_count": 5,
            "application_count": 12
        }
    ],
    "count": 1
}
```

**Parameters:**
- `query` (required): Search query string (max 500 chars)
- `limit` (optional): Max results (default: 10)
- `agent_name` (optional): Filter by agent
- `status` (optional): Filter by status

#### Operation: `list`

List lessons with filtering, pagination, and sorting.

```python
await agent.call_tool("lessons_learned", {
    "operation": "list",
    "agent_name": "my_agent",
    "status": "active",
    "category": "error_pattern",
    "limit": 20,
    "offset": 0,
    "sort_by": "confidence",
    "sort_order": "desc"
})
```

**Response:**
```json
{
    "lessons": [
        {
            "lesson_id": "les_001",
            "title": "Always validate user input before processing",
            "confidence": 0.75,
            "status": "active",
            "category": "error_pattern",
            "priority": 8,
            "evidence_count": 5,
            "created_at": "2024-01-29T14:30:22.123Z"
        }
    ],
    "total": 42,
    "limit": 20,
    "offset": 0
}
```

**Parameters:**
- `agent_name` (optional): Filter by agent
- `status` (optional): Filter by status (`draft`, `active`, `inactive`, `archived`)
- `category` (optional): Filter by category
- `limit` (optional): Results per page (default: 50)
- `offset` (optional): Pagination offset (default: 0)
- `sort_by` (optional): Sort field (default: `created_at`)
- `sort_order` (optional): `asc` or `desc` (default: `desc`)

#### Operation: `update`

Update an existing lesson.

```python
await agent.call_tool("lessons_learned", {
    "operation": "update",
    "lesson_id": "les_001",
    "title": "Always validate user input with clear error messages",
    "status": "active",
    "priority": 9
})
```

**Response:**
```json
{
    "lesson_id": "les_001",
    "updated_fields": ["title", "status", "priority"],
    "message": "Lesson updated successfully"
}
```

**Parameters:**
- `lesson_id` (required): Lesson ID to update
- `title`, `content`, `category`, `priority`, `status`, `tags`, `source_type` (all optional) — only provided fields are updated

#### Operation: `confirm`

Add evidence that a lesson is correct or incorrect.

```python
# Add confirming evidence
await agent.call_tool("lessons_learned", {
    "operation": "confirm",
    "lesson_id": "les_001",
    "evidence_type": "confirm",
    "description": "Applied input validation to checkout form, prevented 15 error reports this week"
})

# Add contradicting evidence
await agent.call_tool("lessons_learned", {
    "operation": "confirm",
    "lesson_id": "les_002",
    "evidence_type": "contradict",
    "description": "User complained validation was too strict, blocking legitimate submissions"
})
```

**Response:**
```json
{
    "lesson_id": "les_001",
    "evidence_type": "confirm",
    "new_confidence": 0.82,
    "previous_confidence": 0.75,
    "total_evidence": 6,
    "message": "Evidence recorded, confidence updated"
}
```

**Parameters:**
- `lesson_id` (required): Lesson ID
- `evidence_type` (required): `confirm`, `contradict`, or `neutral`
- `description` (optional): Details about this evidence

#### Operation: `teach`

Share a lesson with another agent (cross-agent teaching).

```python
await agent.call_tool("lessons_learned", {
    "operation": "teach",
    "target_agent": "coding_agent",
    "title": "Always validate user input before processing",
    "content": "User reported errors when submitting empty forms...",
    "category": "error_pattern",
    "priority": 8
})
```

**Response:**
```json
{
    "lesson_id": "les_042",
    "target_agent": "coding_agent",
    "source_agent": "my_agent",
    "message": "Lesson taught to coding_agent successfully"
}
```

**Parameters:**
- `target_agent` (required): Agent to teach the lesson to
- `title`, `content`, `category`, `priority`, `tags` (same as `store` operation)

## Hooks

### Inject Lessons Hook

**Hook Type:** `pre_llm_call`  
**Priority:** After context optimization, before message validation  
**Default:** Disabled (enable per-agent)

Automatically appends relevant active lessons to the history before LLM calls.

**How it works:**
1. Fetches all active lessons for the current agent
2. Sorts by priority (high), confidence (high), evidence count (high)
3. Builds a formatted prompt section with lesson titles + content
4. Appends a `developer` message with `injected_by="lessons_learned"` at the end -- only when the text differs from the block written last; the earlier block stays where it is
5. Writes nothing at all when the text is unchanged -- and never removes an earlier block: deleting it would rewrite the prefix the provider has already cached

**Injected Format:**
```markdown
## 🎓 LESSONS LEARNED

The following lessons have been learned through experience:

### 🔧 Error Patterns
**Always validate user input before processing** (Priority: 8, Confidence: 0.82)
User reported errors when submitting empty forms. Always add input validation with clear error messages...

### 🚀 Performance
**Cache database queries for read-heavy operations** (Priority: 7, Confidence: 0.75)
...
```

**Agent Visibility:** The injected message is visible to the agent in Message Debugger (`injected_by` field).

### Extract Lessons Hook

**Hook Type:** `session_end`  
**Priority:** Runs at session end (30s timeout)  
**Default:** Disabled (enable per-agent)

Automatically extracts lessons from conversation history using LLM analysis.

**How it works:**
1. Retrieves full conversation history at session end
2. Sends to LLM with extraction prompt (turbo profile by default)
3. LLM identifies patterns, corrections, preferences, mistakes
4. Returns structured JSON with lesson candidates
5. Each candidate goes through deduplication check; a candidate whose check
   cannot run (no embedding model) is skipped, not stored unchecked — it would
   come back as a new copy at every session end
6. New lessons stored with `source_type="auto"` (confidence: 0.4)
7. Similar lessons get merged (evidence added instead of duplicate)
8. Logs extraction attempt (success/failure, token usage)

**What gets extracted:**
- User corrections ("you did X wrong, should have done Y")
- Explicit preferences stated by user
- Patterns that led to successful outcomes
- Mistakes or anti-patterns to avoid
- Domain knowledge shared by user
- Tool usage patterns (worked well/poorly)
- Workflow improvements discovered

**What does NOT get extracted:**
- One-off factual queries ("What is the capital of France?")
- Task-specific details that won't generalize
- Trivially obvious best practices

## The panel

**Lessons Learned** in the launcher under **Context**. It shows the lessons of every agent from the plugin's own store;
what is changed here reaches an agent with the inject hook at its next LLM call.

- Figures: all lessons (or those of the agent filtered for), how many are active, draft, inactive and archived, and
  the number of agents. Filters by agent, status and category, sorted by priority, confidence, evidence, creation or
  last update, 50 to a page. **Refresh** reloads; its auto refresh runs every 30 s once switched on.
- **Search** finds lessons by meaning, in every status, narrowed by the agent, status and category filters, with the
  similarity of each; **Clear** goes back to the list.
- Each lesson shows its title, content, id, tags, agent, category, priority, confidence (green from 0.7, red below
  0.4), status, evidence and application counts and source. **Activate** a draft, **Edit** (the lesson is loaded
  afresh, every field is saved), **Delete** (asks first). **New lesson** opens the same editor.
- **Consolidate** groups similar lessons of one agent or all and lets the consolidation LLM merge each group into its
  best lesson, deleting the others; progress is shown while it runs, and the list is loaded anew after a merge, also
  one that failed. A dry run (the default) only lists what would be merged. A merge runs to its end even when the
  panel is closed meanwhile, and only one merge runs at a time.
- **Clean up** deletes the lessons matching every filter set (agent, status, age, evidence at most, confidence at
  most). **Preview** lists them first; **Delete** takes only the lessons of that preview, never one that came to
  match since. Changing a filter drops the preview.
- What the server refuses -- a lesson gone meanwhile, a draft activated by an agent's evidence -- is shown as an
  error, and the list is loaded anew.

Under `/plugins/lessons_learned/`: `GET lessons` (`agent_name`, `status`, `category`, `sort_by`, `limit`, `offset`),
`POST lessons/search`, `GET|PUT|DELETE lessons/{lesson_id}`, `POST lessons` (create), `POST
lessons/{lesson_id}/activate`, `GET stats` (`agent_name`), `GET categories`, `POST consolidate` (NDJSON: `progress`
lines, then one `result` or `error`), `POST cleanup` (`dry_run`, and `lesson_ids` to narrow a delete), `GET /` (the
panel). A lesson is sent whole and checked (422), without a length limit, so a lesson stored longer before saves back
unchanged; without `tags` the stored tags stay. The store rounds the priority half up into 1 to 10 and splits tags
given as text; the agents' tool refuses a title over 200 or content over 2000 characters and a priority outside 1 to
10. A refusal answers 400 (lesson limit of the agent reached, a cleanup without a filter), 404 (lesson not found) or
409 (activating a lesson that is no longer a draft, a merge while another runs).

## Use Cases

### When to Use Lessons Learned

✅ **Perfect for:**
- Capturing user preferences and corrections
- Learning from mistakes across sessions
- Building agent-specific best practices
- Cross-project knowledge transfer
- Style guide enforcement (tone, formatting, structure)
- Tool usage optimization

❌ **Not ideal for:**
- Session-specific context (use `memory` plugin instead)
- Frequently changing information (use regular context)
- Complex workflows (use `sequential_thinking` instead)

### Example Scenarios

**Coding Agent Learning:**
```
Session 1: User says "Always use TypeScript strict mode"
→ Extract lesson (auto, confidence 0.4)

Session 2: Agent uses strict mode, user happy
→ Add confirm evidence (confidence → 0.6)

Session 3: Agent uses strict mode, catches 3 bugs early
→ Add confirm evidence (confidence → 0.75)

Future sessions: Lesson automatically injected, agent follows it
```

**Writing Agent Learning:**
```
Session 1: User corrects "Use active voice, not passive"
→ Extract lesson

Session 2: Agent uses passive voice, user frustrated
→ Add contradict evidence to old lesson, extract new one

Session 3: Agent consistently uses active voice
→ Add confirm evidence (confidence → 0.8)
```

## Examples

### Manual Lesson Creation

```python
# Store a style preference
await agent.call_tool("lessons_learned", {
    "operation": "store",
    "title": "User prefers concise responses without verbose explanations",
    "content": "User said 'please be more direct and skip the detailed explanations'. Keep responses short and to the point unless explicitly asked for details.",
    "category": "communication",
    "priority": 9,
    "tags": ["style", "user-preference", "brevity"]
})
```

### Search for Relevant Lessons

```python
# Before implementing a feature
lessons = await agent.call_tool("lessons_learned", {
    "operation": "search",
    "query": "authentication best practices secure passwords",
    "limit": 5
})

# Apply relevant lessons from search results
for lesson in lessons["results"]:
    if lesson["confidence"] > 0.6:
        print(f"Apply: {lesson['title']}")
```

### Record Evidence

```python
# Lesson worked well
await agent.call_tool("lessons_learned", {
    "operation": "confirm",
    "lesson_id": "les_042",
    "evidence_type": "confirm",
    "description": "Used the recommended approach, completed task 40% faster"
})

# Lesson didn't work
await agent.call_tool("lessons_learned", {
    "operation": "confirm",
    "lesson_id": "les_042",
    "evidence_type": "contradict",
    "description": "Approach caused unexpected errors with edge cases"
})
```

### Cross-Agent Teaching

```python
# Senior agent teaches junior agent
await agent.call_tool("lessons_learned", {
    "operation": "teach",
    "target_agent": "junior_coder",
    "title": "Always write unit tests before implementing features",
    "content": "TDD approach catches bugs earlier and leads to better design. Requirements: Write test first, implement minimal code to pass, refactor.",
    "category": "workflow",
    "priority": 8
})
```

### Enable Automatic Features

```yaml
# config/agents/my_agent.yaml
my_agent:
  agent_config:
    llm_profile: chat
    hooks:
      lessons_learned.inject_lessons:
        enabled: true  # Auto-inject lessons into prompts
      lessons_learned.extract_lessons:
        enabled: true  # Auto-extract at session end
```

## Implementation Notes

### Deduplication Strategy

When storing a new lesson:
1. Generate embedding for title + content
2. Search for similar lessons (threshold: 0.82)
3. If similarity > 0.95 (exact duplicate): Add evidence to existing lesson
4. If 0.82 < similarity < 0.95: Return conflict warning, let user decide
5. If similarity < 0.82: Store as new lesson

Without the embedding model (a core install without torch) no vector call
works, and every answer says so instead of passing for a verdict: `store` and
`teach` still store the row but return `warnings` (no duplicate check ran; not
in the semantic index, so search will not find it — `list` does), and `search`
returns an error rather than zero results. A search where only some agents'
indexes fail returns the hits it has plus `warnings` naming the others.

Once the store works again, the first search, duplicate check or consolidation
of an agent in a process reconciles that agent's collection with the rows
(`_heal_index`): lessons with a row but no vector are indexed, a vector made of
another text than the row holds (its `text_hash` metadata; a re-index that
failed) is re-embedded, and vectors whose row is gone (a delete that failed) or
belongs to an agent of another collection (a move that failed halfway) are
removed — judged per collection, since two agent names can share one. Vectors
written before `text_hash` existed are re-embedded once. Any failed vector
write or delete makes the next query of that agent reconcile again.

### Confidence Dynamics

Confidence is **not static** — it updates based on:
- **Evidence**: Confirms increase, contradicts decrease
- **Effectiveness**: Successful applications increase
- **Manual override**: the panel's editor sets confidence directly

**No time-based decay** — lessons don't lose confidence over time. Outdated lessons should be explicitly marked as `contradict` or archived.

### Status Lifecycle

```
draft → active → inactive → archived
         ↓         ↑
    (auto-deactivate if confidence < 0.2)
    (auto-reactivate if confidence > 0.4)
```

### Performance Considerations

- **Vector search**: Fast (<100ms for typical queries)
- **Injection overhead**: Minimal (~50-200ms, cached embeddings)
- **Extraction**: Expensive (~1-3 seconds, runs async at session end)
- **Database**: SQLite with indexes, optimized for read-heavy workloads

## Troubleshooting

### Lessons not appearing in prompts

Check:
1. Hook enabled for agent: `lessons_learned.inject_lessons.enabled: true`
2. Lessons are `active` status (not draft/inactive/archived)
3. Lessons belong to the current agent
4. Confidence above auto-deactivate threshold (0.2)

### Extraction not working

Check:
1. Hook enabled: `lessons_learned.extract_lessons.enabled: true`
2. Session has enough messages (>5 messages recommended)
3. Check extraction logs in database: `SELECT * FROM extraction_log ORDER BY created_at DESC LIMIT 10;`
4. LLM profile configured (`llm_profile: turbo` in config)

### Duplicate lessons appearing

- Adjust `dedup_similarity_threshold` (default: 0.82)
- Lower = stricter deduplication (more duplicates caught)
- Higher = looser deduplication (more false positives)

### Confidence not updating

- Check evidence records: `SELECT * FROM lesson_evidence WHERE lesson_id = 'les_XXX';`
- Verify evidence type is correct (`confirm`, `contradict`, `neutral`)
- Confidence recalculated on every evidence addition

## Related Plugins

- **memory**: For session-specific context (facts, entities, events)
- **sequential_thinking**: For complex multi-step reasoning
- **cognitive_stack**: For high-level task context
- **todo**: For task tracking across sessions

## Development

### Database Access

```bash
# Open SQLite database
sqlite3 data/lessons_learned/lessons.db

# Query lessons
SELECT lesson_id, title, confidence, status, evidence_count 
FROM lessons 
WHERE agent_name = 'my_agent' AND status = 'active'
ORDER BY confidence DESC;
```

### Testing

```bash
# Run lessons_learned tests
pytest tests/plugins/test_plugin_lessons_learned.py -v

# Run with coverage
pytest tests/plugins/test_plugin_lessons_learned.py --cov=src.plugins.lessons_learned --cov-report=html
```

### CLI Tool

```bash
# Access via CLI (if available)
.venv/Scripts/lessons_learned.exe --help
```

## API Reference

See [schema.yaml](schema.yaml) for complete tool/hook/config schemas.

## License

Part of AgentSystem - see main project LICENSE.
