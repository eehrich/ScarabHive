# Sequential Thinking - Response Fields Documentation

**Version**: 1.0.1 (Phase 1 Improvements)  
**Date**: 2025-10-27

---

## Response Format

All `sequentialthinking` tool calls return a structured response with the following fields:

### Core Fields

#### `status` (string)
- **Values**: `"success"` | `"error"`
- **Description**: Overall operation status

#### `session_id` (string)
- **Description**: Unique session identifier
- **Format**: Short alphanumeric ID (e.g., `"abc123"`)
- **Auto-generated**: On first call if not provided

#### `error` (string | null)
- **Description**: Error message if `status="error"`, otherwise `null`

---

### Thought Numbering Fields

#### `current_thought_number` (integer)
- **Description**: Server-assigned global thought number
- **Scope**: Global (monotonically increasing for new thoughts)
- **Behavior**:
  - New thoughts: Auto-incremented (1, 2, 3, ...)
  - Revisions: Uses `revises_thought` number (e.g., revising thought #3 returns `3`)
- **Example**: `5` (this is thought #5 globally)

#### `client_thought_number` (integer)
- **Description**: Client-provided `thought_number` parameter (reference only)
- **Use Case**: Debugging, reconciliation with client's counter
- **Example**: `9` (client sent `thought_number: 9`)

#### `recorded_thoughts_count` (integer) ⭐ NEW
- **Description**: Total number of recorded thought entries (including revisions)
- **Scope**: Global session count
- **Example**: `12` (12 thoughts recorded so far, including revisions)
- **Use Case**: Distinguish between "actual entries" and "estimate"

---

### Progress Fields

#### `total_thoughts_estimate` (integer)
- **Description**: Estimated total thoughts for session
- **Behavior**: 
  - ⭐ **Auto-clamped**: Always `>= actual thoughts` (prevents inconsistencies)
  - Updated via `total_thoughts` parameter
  - Updated via `needs_more_thoughts=true`
- **Example**: `15` (expecting ~15 thoughts total)

#### `progress` (string)
- **Description**: Human-readable progress indicator
- **Format**: `"Thought X/Y"`
- **Behavior**: ⭐ **Always consistent**: Never shows `X > Y`
  - Uses `max(actual_thoughts, total_thoughts_estimate)` as denominator
- **Examples**:
  - `"Thought 5/15"` (5 of 15 thoughts)
  - `"Thought 12/12"` (at estimate, may continue)

#### `next_thought_needed` (boolean)
- **Description**: Whether more thoughts are expected
- **Echo**: Echoes client's parameter
- **Use Case**: UI can hide "continue" prompt if `false`

---

### Branch Fields

#### `branch` (string)
- **Description**: Current active branch
- **Default**: `"main"`
- **Example**: `"alternative_approach"`

#### `branch_summary` (object | null)
- **Description**: Branch tree structure (if branching enabled)
- **Format**:
  ```json
  {
    "main": {
      "parent": null,
      "branched_from": 0,
      "thoughts_count": 5,
      "active": true,
      "created_at": "2025-10-27T10:00:00Z"
    },
    "alternative": {
      "parent": "main",  // ⭐ Derived from branched_from_thought's branch
      "branched_from": 3,
      "thoughts_count": 2,
      "active": false,
      "created_at": "2025-10-27T10:05:00Z"
    }
  }
  ```
- **Note**: `parent` is now correctly derived from the branch of `branched_from_thought`

---

### Validation Fields

#### `warnings` (array[string] | null) ⭐ NEW
- **Description**: Non-fatal validation warnings
- **Format**: Array of warning messages, or `null` if no warnings
- **Examples**:
  ```json
  [
    "total_thoughts (8) < recorded thoughts (10), adjusted to 10",
    "needs_more_thoughts=true but estimate not increased (10 <= 10)",
    "Memory usage: 85% (85/100)"
  ]
  ```
- **Use Cases**:
  - Debugging inconsistent parameters
  - Early warning of memory limits
  - Understanding auto-clamp behavior

---

### History Fields

#### `thought_history` (array[object])
- **Description**: Recent thoughts (last N)
- **Limit**: Configured via `max_summary_thoughts` (default: 10)
- **Format**:
  ```json
  [
    {
      "number": 5,
      "content": "Analyzing the data reveals...",
      "branch": "main",
      "is_revision": false,
      "revises_thought": null,        # ✅ v1.0.2: Always present
      "timestamp": "2025-10-27T10:30:00Z"  # ✅ v1.0.2: Always present
    },
    {
      "number": 3,
      "content": "Revised: Data actually shows...",
      "branch": "main",
      "is_revision": true,
      "revises_thought": 3,           # Points to revised thought
      "timestamp": "2025-10-27T10:35:00Z"
    }
  ]
  ```
- **Note**: As of v1.0.2, `revises_thought` and `timestamp` are always included (consistent with summary)

---

## Example Responses

### Successful Thought (Normal)
```json
{
  "status": "success",
  "session_id": "abc123",
  "current_thought_number": 5,
  "client_thought_number": 5,
  "recorded_thoughts_count": 5,
  "total_thoughts_estimate": 10,
  "next_thought_needed": true,
  "progress": "Thought 5/10",
  "branch": "main",
  "thought_history": [...],
  "branch_summary": {...},
  "error": null,
  "warnings": null
}
```

### Revision
```json
{
  "status": "success",
  "session_id": "abc123",
  "current_thought_number": 3,          // Thought being revised
  "client_thought_number": 9,           // Client's call index
  "recorded_thoughts_count": 10,        // Total entries (incl. this revision)
  "total_thoughts_estimate": 12,
  "next_thought_needed": true,
  "progress": "Thought 10/12",
  "branch": "main",
  "thought_history": [...],
  "branch_summary": {...},
  "error": null,
  "warnings": null
}
```

### Auto-Clamped Estimate (with Warning)
```json
{
  "status": "success",
  "session_id": "abc123",
  "current_thought_number": 13,
  "client_thought_number": 13,
  "recorded_thoughts_count": 13,
  "total_thoughts_estimate": 13,        // Auto-clamped from 8 to 13
  "next_thought_needed": false,
  "progress": "Thought 13/13",          // Consistent display
  "branch": "main",
  "thought_history": [...],
  "branch_summary": {...},
  "error": null,
  "warnings": [
    "total_thoughts (8) < recorded thoughts (13), adjusted to 13"
  ]
}
```

### Multiple Warnings
```json
{
  "status": "success",
  "session_id": "abc123",
  "current_thought_number": 8,
  "client_thought_number": 8,
  "recorded_thoughts_count": 8,
  "total_thoughts_estimate": 10,
  "next_thought_needed": true,
  "progress": "Thought 8/10",
  "branch": "main",
  "thought_history": [...],
  "branch_summary": {...},
  "error": null,
  "warnings": [
    "needs_more_thoughts=true but estimate not increased (10 <= 10)",
    "Memory usage: 80% (8/10)"
  ]
}
```

---

## Migration Notes (v1.0.0 → v1.0.1)

### New Fields (Non-Breaking)
- ✅ `recorded_thoughts_count`: Total entries including revisions
- ✅ `warnings`: Validation warnings array

### Changed Fields (Non-Breaking)
- `total_thoughts_estimate`: Now auto-clamped to `>= actual_thoughts`
- `progress`: Now always consistent (denominator is `max(actual, estimate)`)
- `branch_summary[].parent`: Now correctly derived from `branched_from_thought`'s branch

### Deprecated Fields
- ⚠️ `warning` (singular string): Replaced by `warnings` array
  - **Old**: `"warning": "Some message"` or `null`
  - **New**: `"warnings": ["Message 1", "Message 2"]` or `null`
  - **Migration**: Check for `warnings` (array) instead of `warning` (string)

---

## Best Practices

### For LLMs
1. **Ignore `client_thought_number`**: Use `current_thought_number` as the canonical value
2. **Monitor `warnings`**: Check for validation issues and adjust parameters
3. **Use `recorded_thoughts_count`**: To distinguish "total entries" from "estimate"
4. **Trust `progress`**: Display string is always consistent

### For UI Developers
1. **Display `progress`**: Ready-to-use format
2. **Show `warnings`**: Help users understand auto-corrections
3. **Use `branch_summary.parent`**: Now correctly reflects branch hierarchy
4. **Handle `recorded_thoughts_count`**: Show "X entries, estimated Y total"

### For API Consumers
1. **Check `warnings` for debugging**: Non-fatal issues are logged here
2. **Don't rely on exact estimate**: May be auto-clamped for consistency
3. **Use `current_thought_number`**: Server-assigned, no gaps guaranteed
