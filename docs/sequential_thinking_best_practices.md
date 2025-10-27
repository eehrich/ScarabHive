# Sequential Thinking - Best Practices Guide

**Version**: 1.1.0  
**Date**: 2025-10-27  
**Audience**: LLMs, API Consumers, Developers

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Understanding Thought Numbering](#understanding-thought-numbering)
3. [Branching Strategies](#branching-strategies)
4. [Revision Patterns](#revision-patterns)
5. [Progress Management](#progress-management)
6. [Error Handling](#error-handling)
7. [Performance Optimization](#performance-optimization)
8. [Common Pitfalls](#common-pitfalls)

---

## Quick Start

### Basic Linear Reasoning

```python
# Thought 1
result1 = await tool.sequentialthinking({
    "thought": "First, let's analyze the problem...",
    "thought_number": 1,
    "total_thoughts": 3,
    "next_thought_needed": True
})
session_id = result1["session_id"]  # Save for subsequent calls

# Thought 2
result2 = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "Next, consider the constraints...",
    "thought_number": 2,
    "total_thoughts": 3,
    "next_thought_needed": True
})

# Thought 3
result3 = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "Finally, the solution is...",
    "thought_number": 3,
    "total_thoughts": 3,
    "next_thought_needed": False
})
```

---

## Understanding Thought Numbering

### Field Semantics

| Field | Meaning | Scope | Changes on Revision? |
|-------|---------|-------|----------------------|
| `current_thought_number` | Server-assigned logical thought number | Global | ✅ Yes (uses `revises_thought`) |
| `client_thought_number` | Your request's `thought_number` param | Request | ❌ No (echo) |
| `recorded_thoughts_count` | Total entries (incl. revisions) | Global | ✅ Yes (increments) |
| `total_thoughts_estimate` | Estimated total thoughts | Global | ⚙️ Only if updated |

### Example: Normal Sequence

```python
# Thought 1
{
  "current_thought_number": 1,
  "client_thought_number": 1,
  "recorded_thoughts_count": 1
}

# Thought 2
{
  "current_thought_number": 2,
  "client_thought_number": 2,
  "recorded_thoughts_count": 2
}
```

### Example: Revision

```python
# Original Thought 2
{
  "current_thought_number": 2,
  "recorded_thoughts_count": 2
}

# Revise Thought 2 (your 4th call)
{
  "current_thought_number": 2,      # Points to revised thought
  "client_thought_number": 4,       # Your request counter
  "recorded_thoughts_count": 4      # Total entries (1,2,3,revision-of-2)
}
```

**💡 Best Practice**: Use `current_thought_number` as canonical thought ID, ignore `client_thought_number` for logic.

---

## Branching Strategies

### When to Branch

✅ **Good Use Cases**:
- Exploring alternative approaches
- Testing different assumptions
- Parallel solution paths
- "What-if" scenarios

❌ **Avoid Branching For**:
- Linear corrections (use revisions instead)
- Simple next steps (continue main branch)
- Too many branches (cognitive overload)

### Creating a Branch

**Syntax**: Set BOTH `branch_from_thought` + `branch_id`

```python
# Main branch: Thoughts 1-3
result = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "Alternative approach: Use algorithm B instead...",
    "thought_number": 4,
    "total_thoughts": 5,
    "next_thought_needed": True,
    "branch_from_thought": 2,           # Branch from thought #2
    "branch_id": "algorithm_b"          # New branch name
})

# Server response:
{
  "branch": "algorithm_b",              # Now active
  "branch_summary": {
    "main": {"parent": null, ...},
    "algorithm_b": {
      "parent": "main",                 # Derived from thought #2's branch
      "branched_from": 2
    }
  }
}
```

**✅ Do**:
- Use descriptive branch names (`cloud_first`, `alternative_approach`)
- Branch early if you see diverging paths
- Document branch purpose in first thought

**❌ Don't**:
- Use generic names (`branch1`, `test`)
- Create branches without clear purpose
- Branch too late (after 10+ thoughts)

### Switching Branches

**Syntax**: Set ONLY `branch_id` (no `branch_from_thought`)

```python
# Switch back to main
result = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "Returning to main approach...",
    "thought_number": 5,
    "total_thoughts": 7,
    "next_thought_needed": True,
    "branch_id": "main"                 # Switch to existing branch
    # NO branch_from_thought!
})
```

**Error Case**:
```python
# ❌ Branch doesn't exist
result = await tool.sequentialthinking({
    "branch_id": "nonexistent_branch"   # Error!
})
# → {"status": "error", "error": "Branch 'nonexistent_branch' not found"}
```

### Branch Navigation Example

```python
# 1. Start on main
thought1 = await tool.sequentialthinking({...})  # main, thought 1

# 2. Create alternative branch from thought 1
thought2 = await tool.sequentialthinking({
    "branch_from_thought": 1,
    "branch_id": "alternative",
    ...
})  # alternative, thought 2

# 3. Continue alternative
thought3 = await tool.sequentialthinking({...})  # alternative, thought 3

# 4. Switch back to main
thought4 = await tool.sequentialthinking({
    "branch_id": "main",
    ...
})  # main, thought 4

# 5. Create another branch from thought 4
thought5 = await tool.sequentialthinking({
    "branch_from_thought": 4,
    "branch_id": "edge_cases",
    ...
})  # edge_cases, thought 5
```

**Result Branch Tree**:
```
main (1, 4)
├── alternative (2, 3)
└── edge_cases (5)
```

---

## Revision Patterns

### When to Revise

✅ **Good Use Cases**:
- Correct factual errors
- Refine unclear reasoning
- Add missing details
- Update based on new information

❌ **Avoid Revisions For**:
- New thoughts (just continue)
- Alternative approaches (use branching)
- Completely different direction (create new branch)

### Revising a Thought

**Syntax**: Set `is_revision=true` + `revises_thought=<number>`

```python
# Original thought 2
result2 = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "The data shows X...",
    "thought_number": 2,
    "total_thoughts": 5,
    "next_thought_needed": True
})

# ... later, realize thought 2 was wrong

# Revise thought 2 (your 6th call)
result_rev = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "CORRECTION: The data actually shows Y, not X...",
    "thought_number": 6,                # Your call counter
    "total_thoughts": 7,
    "next_thought_needed": True,
    "is_revision": True,                # Mark as revision
    "revises_thought": 2                # Which thought to revise
})

# Response:
{
  "current_thought_number": 2,          # Points to revised thought
  "client_thought_number": 6,
  "recorded_thoughts_count": 6          # Total entries including revision
}
```

### Revision in thought_history

```json
{
  "thought_history": [
    {
      "number": 2,
      "content": "The data shows X...",
      "is_revision": false,
      "revises_thought": null,
      "timestamp": "2025-10-27T10:00:00Z"
    },
    {
      "number": 2,                      // Same number!
      "content": "CORRECTION: ...",
      "is_revision": true,
      "revises_thought": 2,
      "timestamp": "2025-10-27T10:05:00Z"
    }
  ]
}
```

**💡 Tip**: Use latest `timestamp` to identify most recent version.

### Multiple Revisions

```python
# Original thought 3
thought3_v1 = await tool.sequentialthinking({
    "thought": "Version 1",
    "thought_number": 3,
    ...
})

# First revision
thought3_v2 = await tool.sequentialthinking({
    "thought": "Version 2 (revised)",
    "is_revision": True,
    "revises_thought": 3,
    ...
})

# Second revision
thought3_v3 = await tool.sequentialthinking({
    "thought": "Version 3 (revised again)",
    "is_revision": True,
    "revises_thought": 3,              # Still revises thought 3
    ...
})
```

**thought_history will contain**:
- 3 entries with `number=3`
- Different `timestamp`s
- Last one has `revision_count=2` in summary

---

## Progress Management

### Dynamic Estimation

**Initial Estimate**:
```python
result1 = await tool.sequentialthinking({
    "thought": "Start analyzing...",
    "thought_number": 1,
    "total_thoughts": 5,                # Initial estimate
    "next_thought_needed": True
})
```

**Updating Estimate**:
```python
# Realized it's more complex, need ~10 thoughts
result5 = await tool.sequentialthinking({
    "session_id": session_id,
    "thought": "This is more complex than expected...",
    "thought_number": 5,
    "total_thoughts": 10,               # Updated estimate
    "next_thought_needed": True,
    "needs_more_thoughts": True         # Optional: signals increase
})

# Response:
{
  "total_thoughts_estimate": 10,
  "progress": "Thought 5/10",
  "warnings": null                      # No warning (estimate increased properly)
}
```

### Auto-Clamping

**What happens if estimate < actual?**

```python
# You're at thought 12 but try to set estimate to 8
result = await tool.sequentialthinking({
    "thought_number": 12,
    "total_thoughts": 8,                # ❌ Less than actual!
    ...
})

# Response:
{
  "total_thoughts_estimate": 12,        # Auto-clamped to 12
  "progress": "Thought 12/12",          # Consistent display
  "warnings": [
    "total_thoughts (8) < recorded thoughts (12), adjusted to 12"
  ]
}
```

**💡 Best Practice**: Always increase estimates, never decrease below actual progress.

### Progress Display

**Format**: `"Thought X/Y"`
- `X` = `actual_thoughts` (monotonic counter)
- `Y` = `max(actual_thoughts, total_thoughts_estimate)` (never shows `X > Y`)

```python
# Examples:
"Thought 3/10"      # 3 of 10 estimated
"Thought 8/8"       # At estimate, may continue
"Thought 12/12"     # Auto-clamped (was 12/10)
```

---

## Error Handling

### Common Errors

#### 1. Revision without Target

```python
# ❌ Error: Missing revises_thought
result = await tool.sequentialthinking({
    "is_revision": True,                # Missing revises_thought!
    ...
})

# Response:
{
  "status": "error",
  "error": "revises_thought is required when is_revision=true"
}
```

**Fix**: Always provide `revises_thought` when `is_revision=True`.

#### 2. Revising Non-Existent Thought

```python
# ❌ Error: Thought #99 doesn't exist
result = await tool.sequentialthinking({
    "is_revision": True,
    "revises_thought": 99,              # Doesn't exist!
    ...
})

# Response:
{
  "status": "error",
  "error": "Thought #99 not found in session"
}
```

**Fix**: Check `thought_history` or `summary` before revising.

#### 3. Unknown Branch

```python
# ❌ Error: Branch doesn't exist
result = await tool.sequentialthinking({
    "branch_id": "unknown_branch",
    ...
})

# Response:
{
  "status": "error",
  "error": "Branch 'unknown_branch' not found"
}
```

**Fix**: Check `branch_summary` before switching, or create with `branch_from_thought`.

#### 4. Empty Thought Content

```python
# ❌ Error: Empty content
result = await tool.sequentialthinking({
    "thought": "",                      # Empty!
    ...
})

# Response:
{
  "status": "error",
  "error": "thought content cannot be empty"
}
```

### Warnings vs Errors

**Warnings** (non-fatal, operation succeeds):
```json
{
  "status": "success",
  "warnings": [
    "total_thoughts (5) < recorded thoughts (8), adjusted to 8",
    "needs_more_thoughts=true but estimate not increased (10 <= 10)",
    "Memory usage: 85% (85/100)"
  ]
}
```

**Errors** (fatal, operation fails):
```json
{
  "status": "error",
  "error": "revises_thought is required when is_revision=true"
}
```

**💡 Best Practice**: Always check `status` first, then inspect `warnings` for debugging.

---

## Performance Optimization

### Memory Management

**Default Limit**: 100 thoughts per session (configurable)

```python
# Monitor memory usage via warnings
result = await tool.sequentialthinking({...})

if result.get("warnings"):
    for warning in result["warnings"]:
        if "Memory usage" in warning:
            print(f"⚠️ {warning}")
            # Consider clearing old sessions or summarizing
```

**When approaching limit (>80%)**:
1. Get summary: `await tool.sequential_thinking_get_summary({...})`
2. Save summary externally
3. Clear session: `await tool.sequential_thinking_clear_history({...})`
4. Start new session with summary as context

### Session Cleanup

**Manual Cleanup**:
```python
# Clear specific session
await tool.sequential_thinking_clear_history({
    "session_id": "abc123"
})

# Clear all sessions
await tool.sequential_thinking_clear_history({})
```

**Auto-Cleanup**: Sessions are automatically removed after TTL (default: 1 hour).

### Summary Usage

```python
# Get detailed summary
summary = await tool.sequential_thinking_get_summary({
    "session_id": session_id,
    "max_thoughts": 20,                 # Limit thoughts returned
    "include_branches": True
})

# Use summary for context without keeping full session in memory
```

---

## Common Pitfalls

### ❌ Pitfall 1: Using client_thought_number for Logic

```python
# ❌ BAD: Don't use client_thought_number
if result["client_thought_number"] == 5:
    do_something()

# ✅ GOOD: Use current_thought_number
if result["current_thought_number"] == 5:
    do_something()
```

**Why**: `client_thought_number` is just an echo of your input, not canonical.

---

### ❌ Pitfall 2: Decreasing Estimates

```python
# ❌ BAD: Decreasing estimate
result1 = await tool.sequentialthinking({
    "total_thoughts": 10,
    ...
})

result5 = await tool.sequentialthinking({
    "total_thoughts": 5,                # ❌ Decreased!
    ...
})

# ✅ GOOD: Only increase or keep same
result5 = await tool.sequentialthinking({
    "total_thoughts": 10,               # Keep or increase
    ...
})
```

**Why**: Auto-clamp will adjust anyway, but creates confusing warnings.

---

### ❌ Pitfall 3: Branching for Linear Corrections

```python
# ❌ BAD: Branching for correction
result3 = await tool.sequentialthinking({
    "thought": "Wait, I made an error in thought 2...",
    "branch_from_thought": 2,
    "branch_id": "correction",          # ❌ Don't branch for corrections!
    ...
})

# ✅ GOOD: Use revision
result3 = await tool.sequentialthinking({
    "thought": "CORRECTION to thought 2: ...",
    "is_revision": True,
    "revises_thought": 2,               # ✅ Revise instead
    ...
})
```

**Why**: Revisions update in-place, branches are for alternative approaches.

---

### ❌ Pitfall 4: Not Checking branch_summary

```python
# ❌ BAD: Blindly switching
result = await tool.sequentialthinking({
    "branch_id": "some_branch",         # May not exist!
    ...
})

# ✅ GOOD: Check first
if "some_branch" in result["branch_summary"]:
    result = await tool.sequentialthinking({
        "branch_id": "some_branch",
        ...
    })
else:
    # Create or handle error
```

---

### ❌ Pitfall 5: Ignoring Warnings

```python
# ❌ BAD: Ignoring warnings
result = await tool.sequentialthinking({...})
# No check for warnings

# ✅ GOOD: Log and address
result = await tool.sequentialthinking({...})
if result.get("warnings"):
    for warning in result["warnings"]:
        logger.warning(f"Sequential Thinking: {warning}")
        # Adjust parameters if needed
```

**Why**: Warnings indicate parameter mismatches or approaching limits.

---

## Cheat Sheet

### Quick Reference

| Task | Syntax |
|------|--------|
| **Start session** | `thought_number=1`, omit `session_id` |
| **Continue session** | Include `session_id` from previous response |
| **Create branch** | `branch_from_thought=X`, `branch_id="name"` |
| **Switch branch** | `branch_id="name"` (omit `branch_from_thought`) |
| **Revise thought** | `is_revision=True`, `revises_thought=X` |
| **Update estimate** | Change `total_thoughts`, optionally `needs_more_thoughts=True` |
| **Get summary** | `await tool.sequential_thinking_get_summary({...})` |
| **Clear session** | `await tool.sequential_thinking_clear_history({...})` |

### Response Fields Priority

1. **Always check**: `status` ("success" or "error")
2. **For errors**: Read `error` message
3. **For success**: Use `current_thought_number` (canonical)
4. **Monitor**: `warnings` array for issues
5. **Ignore**: `client_thought_number` (informational only)

---

## Examples Collection

### Example 1: Linear Reasoning with Dynamic Estimate

```python
# Start with estimate of 3
r1 = await tool.sequentialthinking({
    "thought": "Let's break down the problem...",
    "thought_number": 1,
    "total_thoughts": 3,
    "next_thought_needed": True
})
sid = r1["session_id"]

# Thought 2
r2 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Analyzing component A...",
    "thought_number": 2,
    "total_thoughts": 3,
    "next_thought_needed": True
})

# Realized more complex → increase estimate
r3 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "This is more complex than expected, need to analyze B, C, D...",
    "thought_number": 3,
    "total_thoughts": 6,                # Increased estimate
    "next_thought_needed": True,
    "needs_more_thoughts": True
})

# Continue with new estimate
r4 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Analyzing component B...",
    "thought_number": 4,
    "total_thoughts": 6,
    "next_thought_needed": True
})
# ... and so on
```

### Example 2: Branching to Explore Alternatives

```python
# Main approach: thoughts 1-3
r1 = await tool.sequentialthinking({
    "thought": "Using algorithm A...",
    "thought_number": 1,
    "total_thoughts": 5,
    "next_thought_needed": True
})
sid = r1["session_id"]

r2 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Algorithm A step 1...",
    "thought_number": 2,
    "total_thoughts": 5,
    "next_thought_needed": True
})

# Branch to explore algorithm B
r3 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Let's also try algorithm B as alternative...",
    "thought_number": 3,
    "total_thoughts": 7,                # Increased for branch
    "next_thought_needed": True,
    "branch_from_thought": 1,           # Branch from thought 1
    "branch_id": "algorithm_b"
})

# Continue algorithm B branch
r4 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Algorithm B step 1...",
    "thought_number": 4,
    "total_thoughts": 7,
    "next_thought_needed": True
    # Still on algorithm_b (current branch)
})

# Switch back to main
r5 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Back to algorithm A, step 2...",
    "thought_number": 5,
    "total_thoughts": 7,
    "next_thought_needed": True,
    "branch_id": "main"                 # Switch to main
})
```

### Example 3: Revision Workflow

```python
# Thoughts 1-3
r1 = await tool.sequentialthinking({
    "thought": "Premise: Users prefer feature X...",
    "thought_number": 1,
    ...
})
sid = r1["session_id"]

r2 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Based on that, we should prioritize X...",
    "thought_number": 2,
    ...
})

r3 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "Wait, new data shows users actually prefer Y!",
    "thought_number": 3,
    ...
})

# Revise thought 1 (premise was wrong)
r4 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "REVISED: Users prefer feature Y (not X), based on latest survey...",
    "thought_number": 4,
    "total_thoughts": 5,
    "next_thought_needed": True,
    "is_revision": True,
    "revises_thought": 1                # Revise premise
})

# Revise thought 2 (conclusion based on wrong premise)
r5 = await tool.sequentialthinking({
    "session_id": sid,
    "thought": "REVISED: We should prioritize Y instead...",
    "thought_number": 5,
    "total_thoughts": 6,
    "next_thought_needed": False,
    "is_revision": True,
    "revises_thought": 2
})
```

---

## Troubleshooting

### Q: My thought_number doesn't match current_thought_number?

**A**: This is expected! `client_thought_number` (your input) is informational. Use `current_thought_number` (server-assigned) as canonical value.

### Q: Progress shows "12/12" but I expected "12/10"?

**A**: Auto-clamp adjusted `total_thoughts_estimate` to 12 (can't be less than actual). Check `warnings` for details.

### Q: Why do I see thought #2 twice in thought_history?

**A**: One is original, one is revision. Check `is_revision` and `timestamp` to distinguish. Latest timestamp = current version.

### Q: Branch parent is wrong?

**A**: ✅ Fixed in v1.0.1. Parent is now derived from `branched_from_thought`'s branch, not `current_branch`.

### Q: How to undo a mistake?

**A**: Use revision (`is_revision=True`, `revises_thought=X`) to correct. No built-in undo (coming in v2.0).

---

## Version History

- **v1.1.0** (2025-10-27): Added best practices guide
- **v1.0.2** (2025-10-27): `thought_history` consistency fix
- **v1.0.1** (2025-10-27): Phase 1 improvements (auto-clamp, warnings, branch-parent fix)
- **v1.0.0** (2025-10-26): Initial release
