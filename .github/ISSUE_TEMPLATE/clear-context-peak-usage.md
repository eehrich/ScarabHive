---
name: Clear context doesn't reset peak usage
about: Peak usage not cleared when context usage history is cleared
title: "Peak usage persists after clearing context history"
labels: bug, context, stats
assignees: ''
---

## Summary

When clearing context usage history via the `/debug/context/usage/clear` endpoint (or the UI clear action), the per-agent `peak_tokens` metric was not reset. This caused the UI to continue displaying the previous peak usage value even after context history and accumulated stats were cleared.

## Steps to Reproduce

1. Use the app to generate LLM calls that increase an agent's token usage.
2. Open the context usage debug page and note the `Peak usage` / `Per-Agent peak_tokens` value.
3. Call the endpoint `/debug/context/usage/clear` (or use the UI clear action).
4. Reload the context usage view — the peak usage still shows the previous high value.

## Expected

Clearing context usage history should reset `peak_tokens` to zero (or recompute from an empty history), so the UI reflects no peak usage after the clear action.

## Fix

Reset `stats.peak_tokens = 0` for each agent when handling the clear action. A PR was applied to `src/agent_system/agent/interface_api.py` to set `stats.peak_tokens = 0` during the clear flow.

## Additional notes

- The accumulator persistent stats were already reset by calling `acc.reset_all_stats()`.
- The change is safe and only affects the in-memory agent tracker state; persistent accumulator data is reset separately.

## Patch

File: `src/agent_system/agent/interface_api.py`
- Reset `stats.peak_tokens = 0` for each agent in the clear handler.

## Related

- `src/agent_system/context/accumulator.py`

Please attach logs/screenshots if the issue reappears after this fix.
