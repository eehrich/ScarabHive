"""
Migration Plan: Eliminating asyncio.sleep(0) Anti-Pattern
========================================================

## Overview
Systematic replacement of 32+ asyncio.sleep(0) calls with guaranteed delivery patterns.

## Analysis of Current asyncio.sleep(0) Usage

### Critical Status Publishing Locations (High Priority):
- src/agent_system/servers/agent/server.py: 11 instances
  - Lines 1141, 1166, 1174, 1186, 1273, 1289, 1376, 1609, 1617
  - All related to status message delivery guarantees

- src/plugins/web_research_agent/server.py: 17 instances  
  - Lines 199, 233, 236, 258, 268, 291, 304, 310, 351, 365, 377, 402, 415, 426, 455, 468, 478
  - All related to status and progress publishing

### Non-Critical Locations (Lower Priority):
- src/agent_system/context/optimizer.py: 3 instances
  - Lines 65, 93, 97
  - Context optimization workflow

- tests/test_cli_streaming_raw.py: 1 instance
  - Line 20
  - Test harness timing

## Migration Strategy (Phase-by-Phase)

### Phase 1: Infrastructure Setup ✅ DONE
- [✅] Created ImprovedStatusBus with StatusPipeline
- [✅] Created status_scope context manager
- [✅] Created drop-in publish_status_improved() function

### Phase 2: Core Agent Server Migration (HIGH PRIORITY)
- Replace all asyncio.sleep(0) in server.py with guaranteed delivery
- Focus on status publishing in run_events() method
- Ensure final status messages are guaranteed delivered

### Phase 3: WebResearchAgent Migration (HIGH PRIORITY)  
- Replace status publishing pattern in _run_with_progress()
- Use status_scope context manager for automatic START/END
- Eliminate all 17 asyncio.sleep(0) calls

### Phase 4: MainAgent Integration (MEDIUM PRIORITY)
- Update MainAgent to use status_scope for dual status
- Replace manual coordination with automatic patterns

### Phase 5: Context Optimizer (LOW PRIORITY)
- Analyze context optimization timing dependencies
- Replace with deterministic coordination patterns

### Phase 6: Test Cleanup (LOW PRIORITY)
- Update test harness timing patterns
- Ensure tests work with guaranteed delivery

## Implementation Details

### New Patterns to Use:

1. **Instead of:**
   ```python
   await publish_status("server", "message", request_id, PHASE_START)
   await asyncio.sleep(0)
   ```
   
   **Use:**
   ```python
   await publish_status("server", "message", request_id, StatusPhase.START)
   ```

2. **For dual agent coordination:**
   ```python
   async with status_scope(
       status_bus,
       name="agent",
       request_id=request_id
   ) as status:
       await status.step("Working...")
       # START/END automatically handled
   ```

3. **For simple migrations:**
   ```python
   # Replace: await publish_status(...); await asyncio.sleep(0)
   # With:    await publish_status(...)
   ```

## Risk Assessment

### Low Risk:
- Drop-in function replacement (publish_status_improved)
- Context manager usage (automatic START/END)

### Medium Risk:  
- Changing event loop timing might expose other race conditions
- Need thorough testing of status message ordering

### High Risk:
- Context optimizer timing dependencies may be critical
- Test timing might need adjustment

## Validation Strategy

1. **Unit Tests**: All existing status tests must pass
2. **Integration Tests**: Web UI status display must work
3. **Real-world Testing**: Run actual agent tasks with status monitoring
4. **Performance Testing**: Ensure no significant latency increase

## Success Metrics

- ✅ Zero asyncio.sleep(0) calls in status publishing code
- ✅ All status messages guaranteed delivered 
- ✅ Web UI shows correct dual status for all agents
- ✅ No timing-dependent race conditions
- ✅ Improved system reliability and maintainability
- ✅ Clean status API with modern async patterns

## Migration Completed

The migration from the old status system to the new clean `status` module has been completed:
- Old `improved_status.py` has been replaced with clean `status.py`
- All imports updated across the codebase
- Examples and documentation updated
- Tests adapted to use the new API
- No backward compatibility layer needed due to clean design
"""