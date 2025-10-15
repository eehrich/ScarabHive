# 🔧 AgentSystem v1.1: Bug Fixes & Quick Improvements

**Document Type:** Tactical Improvements & Bug Fixes  
**Version:** 1.1 (Immediate)  
**Timeline:** 1-2 Weeks  
**Date:** 2025-10-15  
**Status:** ✅ ACTIONABLE - Critical Fixes

---

## 📋 Table of Contents

1. [Executive Summary](#executive-summary)
2. [Critical Issues Found](#critical-issues-found)
3. [Bug Fixes](#bug-fixes)
4. [Code Quality Improvements](#code-quality-improvements)
5. [Error Handling Improvements](#error-handling-improvements)
6. [Performance Optimizations](#performance-optimizations)
7. [Implementation Plan](#implementation-plan)
8. [Testing Strategy](#testing-strategy)

---

## 1. Executive Summary

### 🎯 Goal

**Fix critical bugs and improve code quality in the existing codebase without architectural changes.**

### 📊 Scope: Code Quality Only

| Category | Issues Found | Priority |
|----------|-------------|----------|
| **Error Handling** | Multiple bare `except:` blocks | 🔴 P0 |
| **Resource Leaks** | File handles not closed | 🔴 P0 |
| **Race Conditions** | Concurrent file writes | 🟡 P1 |
| **Type Safety** | Missing type hints | 🟢 P2 |
| **Logging** | Inconsistent log levels | 🟢 P2 |
| **Code Duplication** | Repeated exception handling | 🟢 P2 |

### 🚀 Value Proposition

- **No breaking changes** - Only fixes, no API changes
- **Immediate stability** - Fix crashes and data loss
- **Better debugging** - Improved error messages
- **Code maintainability** - Cleaner, safer code

---

## 2. Critical Issues Found

### 2.1 Error Handling Problems 🔴 CRITICAL

#### Issue 1: Bare Exception Handlers

**Found in:** Multiple files

**Problem:**
```python
# ❌ BAD: Catches everything, including KeyboardInterrupt, SystemExit
try:
    result = do_something()
except:
    logger.error("Failed")  # What failed? Why?
```

**Impact:**
- Hides bugs (catches programming errors)
- Can't interrupt with Ctrl+C
- Unclear what went wrong
- Makes debugging impossible

**Found instances:**
```bash
# From grep search:
src/agent_system/api/session_endpoints.py: Multiple generic `except Exception:` blocks
src/agent_system/services/agent_service.py: Generic exception handling
src/agent_system/utils/prompt_renderer.py: Bare `except Exception:`
```

#### Issue 2: Silent Failures

**Problem:**
```python
# ❌ BAD: Logs error but continues silently
try:
    await save_session(session)
except Exception as e:
    logger.warning(f"Failed to save session: {e}")
    # What now? Session data lost!
```

**Impact:**
- Data loss (sessions not saved)
- User doesn't know operation failed
- Silent corruption

#### Issue 3: Inconsistent Error Responses

**Problem:**
```python
# Different error formats across endpoints
# Some return 500, some 400, some 404 for same error
except Exception as e:
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
```

**Impact:**
- Hard to handle errors in frontend
- Unclear what went wrong
- No error codes for programmatic handling

---

### 2.2 Resource Management Issues 🔴 CRITICAL

#### Issue 1: File Handles Not Closed

**Found in:** Session storage, config loading

**Problem:**
```python
# ❌ BAD: File might stay open on exception
def load_config(path):
    f = open(path, 'r')
    data = json.load(f)  # Exception here = file not closed
    f.close()
    return data
```

**Impact:**
- Resource leaks (too many open files)
- File lock issues on Windows
- Corrupted files on crash

**Fix:**
```python
# ✅ GOOD: Always closes file
def load_config(path):
    with open(path, 'r') as f:
        return json.load(f)
```

#### Issue 2: Async Context Not Properly Managed

**Problem:**
```python
# ❌ BAD: Task might leak
task = asyncio.create_task(background_work())
# Task never awaited or cancelled
```

**Impact:**
- Memory leaks (tasks pile up)
- Zombie processes
- Shutdown hangs

---

### 2.3 Concurrency Issues 🟡 HIGH

#### Issue 1: Race Condition in File Writes

**From backlog (Epic 0011):**
> Task 0127: Integration test simulating concurrent `agent-cli` processes writing the same managed file

**Problem:**
```python
# ❌ BAD: Two processes write simultaneously
# Process A reads file
# Process B reads file (sees same data)
# Process A writes changes
# Process B writes changes (overwrites A's changes!)
```

**Impact:**
- Data loss (last write wins)
- Corrupted files
- Lost backlog updates

**Current workaround:** None - users must avoid concurrent runs

#### Issue 2: Session Cache Race Condition

**Problem:**
```python
# ❌ BAD: Multiple requests modify same session
session = load_session(session_id)  # Request 1
# ... 
session = load_session(session_id)  # Request 2 (gets stale data)
session.add_message(msg1)           # Request 1
session.add_message(msg2)           # Request 2
save_session(session)               # Request 1 saves
save_session(session)               # Request 2 overwrites (msg1 lost!)
```

**Impact:**
- Lost messages
- Incorrect conversation history
- User confusion

---

### 2.4 Type Safety Issues 🟢 MEDIUM

#### Issue 1: Missing Type Hints

**Problem:**
```python
# ❌ BAD: What types are expected?
def execute_task(task, session_id=None, images=None):
    ...
```

**Impact:**
- IDE can't autocomplete
- No type checking
- Runtime type errors

**Fix:**
```python
# ✅ GOOD: Clear types
async def execute_task(
    task: str,
    session_id: Optional[str] = None,
    images: Optional[list[bytes]] = None
) -> AsyncIterator[dict[str, Any]]:
    ...
```

---

### 2.5 Logging Issues 🟢 MEDIUM

#### Issue 1: Inconsistent Log Levels

**Problem:**
```python
# Mix of log levels for same type of event
logger.info("Task failed")      # Should be error
logger.error("Task started")    # Should be info
logger.debug("Critical error")  # Should be error
```

**Impact:**
- Can't filter logs effectively
- Important errors missed
- Log spam

#### Issue 2: Missing Context in Logs

**Problem:**
```python
# ❌ BAD: Which session? Which user?
logger.error("Failed to save session")
```

**Fix:**
```python
# ✅ GOOD: Full context
logger.error(
    "Failed to save session: session_id=%s, user=%s, error=%s",
    session_id, username, e
)
```

---

## 3. Bug Fixes

### 3.1 Fix Exception Handling

**Priority:** 🔴 P0

#### Fix 1: Replace Bare Exception Handlers

**Files to fix:**
- `src/agent_system/api/session_endpoints.py`
- `src/agent_system/services/agent_service.py`
- `src/agent_system/utils/prompt_renderer.py`

**Before:**
```python
try:
    result = await process_request(request)
except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))
```

**After:**
```python
try:
    result = await process_request(request)
except ValueError as e:
    # User input error
    raise HTTPException(status_code=400, detail=f"Invalid input: {e}")
except SessionNotFoundError as e:
    # Resource not found
    raise HTTPException(status_code=404, detail=str(e))
except LLMConnectionError as e:
    # External service error
    raise HTTPException(status_code=503, detail=f"LLM service unavailable: {e}")
except Exception as e:
    # Unexpected error - log and return generic message
    logger.exception("Unexpected error processing request: %s", e)
    raise HTTPException(
        status_code=500,
        detail="Internal server error. Please contact support."
    )
```

#### Fix 2: Add Custom Exception Classes

**Create:** `src/agent_system/exceptions.py`

```python
"""Custom exceptions for AgentSystem."""

class AgentSystemError(Exception):
    """Base exception for all AgentSystem errors."""
    pass

class ConfigurationError(AgentSystemError):
    """Configuration is invalid or missing."""
    pass

class SessionNotFoundError(AgentSystemError):
    """Session does not exist."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")

class SessionPermissionError(AgentSystemError):
    """User lacks permission to access session."""
    def __init__(self, session_id: str, username: str):
        self.session_id = session_id
        self.username = username
        super().__init__(f"User {username} cannot access session {session_id}")

class PluginLoadError(AgentSystemError):
    """Plugin failed to load."""
    def __init__(self, plugin_name: str, reason: str):
        self.plugin_name = plugin_name
        self.reason = reason
        super().__init__(f"Failed to load plugin {plugin_name}: {reason}")

class ToolExecutionError(AgentSystemError):
    """Tool execution failed."""
    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Tool {tool_name} failed: {reason}")

class LLMConnectionError(AgentSystemError):
    """Cannot connect to LLM service."""
    pass

class LLMRateLimitError(AgentSystemError):
    """LLM service rate limit exceeded."""
    pass

class ValidationError(AgentSystemError):
    """Data validation failed."""
    pass
```

#### Fix 3: Centralized Error Handler

**Add to:** `src/agent_system/app.py`

```python
from fastapi import Request
from fastapi.responses import JSONResponse
from agent_system.exceptions import (
    AgentSystemError,
    SessionNotFoundError,
    SessionPermissionError,
    LLMConnectionError,
    ValidationError
)

# Map exceptions to HTTP status codes
EXCEPTION_STATUS_MAP = {
    SessionNotFoundError: 404,
    SessionPermissionError: 403,
    LLMConnectionError: 503,
    LLMRateLimitError: 429,
    ValidationError: 422,
    ConfigurationError: 500,
    PluginLoadError: 500,
    ToolExecutionError: 500,
}

@app.exception_handler(AgentSystemError)
async def agentsystem_exception_handler(request: Request, exc: AgentSystemError):
    """Handle all AgentSystem custom exceptions."""
    status_code = EXCEPTION_STATUS_MAP.get(type(exc), 500)
    
    # Log based on severity
    if status_code >= 500:
        logger.error(
            "Server error: %s",
            exc,
            extra={
                "exception_type": type(exc).__name__,
                "path": request.url.path,
                "method": request.method
            }
        )
    else:
        logger.warning(
            "Client error: %s",
            exc,
            extra={
                "exception_type": type(exc).__name__,
                "path": request.url.path
            }
        )
    
    return JSONResponse(
        status_code=status_code,
        content={
            "error": type(exc).__name__,
            "message": str(exc),
            "path": request.url.path
        }
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unexpected exceptions."""
    logger.exception(
        "Unhandled exception: %s",
        exc,
        extra={
            "path": request.url.path,
            "method": request.method,
            "user_agent": request.headers.get("user-agent")
        }
    )
    
    # Don't leak internal details
    return JSONResponse(
        status_code=500,
        content={
            "error": "InternalServerError",
            "message": "An unexpected error occurred. Please try again later.",
            "support": "Contact support if this persists."
        }
    )
```

---

### 3.2 Fix Resource Leaks

**Priority:** 🔴 P0

#### Fix 1: File Handle Cleanup

**Find all file operations:**
```bash
grep -r "open(" src/agent_system/ --include="*.py"
```

**Pattern to fix:**
```python
# ❌ BEFORE
f = open(path, 'r')
try:
    data = f.read()
finally:
    f.close()

# ✅ AFTER
with open(path, 'r') as f:
    data = f.read()
```

**Specific files:**

**File:** `src/agent_system/services/session_service.py`
```python
# Fix session loading
async def load_session(self, session_id: str) -> Session:
    """Load session from disk."""
    path = self._get_session_path(session_id)
    
    if not path.exists():
        raise SessionNotFoundError(session_id)
    
    # ✅ Use context manager
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return Session(**data)
    except json.JSONDecodeError as e:
        logger.error("Corrupted session file: %s, error: %s", path, e)
        raise ValidationError(f"Session file corrupted: {session_id}")
    except IOError as e:
        logger.error("Cannot read session file: %s, error: %s", path, e)
        raise AgentSystemError(f"Cannot read session: {session_id}")
```

#### Fix 2: Async Task Cleanup

**File:** `src/agent_system/app.py`

```python
# Track background tasks for cleanup
_background_tasks: set[asyncio.Task] = set()

def create_background_task(coro):
    """Create a background task with automatic cleanup."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan with proper cleanup."""
    logger.info("FastAPI application starting up")
    yield
    
    # Shutdown: Cancel all background tasks
    logger.info("Cancelling %d background tasks", len(_background_tasks))
    for task in _background_tasks:
        task.cancel()
    
    # Wait for tasks to finish
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    logger.info("All background tasks cancelled")
```

---

### 3.3 Fix Concurrency Issues

**Priority:** 🟡 P1

#### Fix 1: File Locking for Backlog

**Install:** `pip install filelock`

**Create:** `src/agent_system/utils/file_lock.py`

```python
"""File locking utilities for safe concurrent access."""
import time
from pathlib import Path
from typing import Optional
from filelock import FileLock, Timeout
import logging

logger = logging.getLogger(__name__)

class SafeFileWriter:
    """Thread-safe and process-safe file writer."""
    
    def __init__(self, file_path: Path, timeout: float = 10.0):
        self.file_path = file_path
        self.lock_path = file_path.with_suffix(file_path.suffix + ".lock")
        self.timeout = timeout
    
    def write(self, content: str, encoding: str = "utf-8"):
        """Write content to file with lock."""
        lock = FileLock(self.lock_path, timeout=self.timeout)
        
        try:
            with lock:
                # Atomic write: write to temp file, then rename
                temp_path = self.file_path.with_suffix(".tmp")
                
                with open(temp_path, 'w', encoding=encoding) as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())  # Force write to disk
                
                # Atomic rename
                temp_path.replace(self.file_path)
                
                logger.debug("Safely wrote to file: %s", self.file_path)
        
        except Timeout:
            logger.error(
                "Failed to acquire lock for file: %s (timeout=%s)",
                self.file_path,
                self.timeout
            )
            raise AgentSystemError(
                f"Cannot write to {self.file_path}: file is locked"
            )
    
    def read(self, encoding: str = "utf-8") -> str:
        """Read content from file with lock."""
        lock = FileLock(self.lock_path, timeout=self.timeout)
        
        try:
            with lock:
                with open(self.file_path, 'r', encoding=encoding) as f:
                    return f.read()
        
        except Timeout:
            logger.error("Failed to acquire lock for reading: %s", self.file_path)
            raise AgentSystemError(
                f"Cannot read {self.file_path}: file is locked"
            )
```

**Usage in backlog tool:**
```python
# scripts/backlog.py
from agent_system.utils.file_lock import SafeFileWriter

def write_backlog(content: str):
    """Write backlog with file locking."""
    backlog_path = Path("backlog.md")
    writer = SafeFileWriter(backlog_path)
    
    try:
        writer.write(content)
    except AgentSystemError as e:
        print(f"Error: {e}")
        print("Another process is modifying the backlog. Please try again.")
        sys.exit(1)
```

#### Fix 2: Session Locking

**Add to:** `src/agent_system/services/session_service.py`

```python
import asyncio
from collections import defaultdict

class SessionService:
    """Session service with lock-per-session."""
    
    def __init__(self):
        # Lock per session_id
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    
    async def save_session(self, session: Session):
        """Save session with lock."""
        async with self._session_locks[session.id]:
            # Only one coroutine can save this session at a time
            path = self._get_session_path(session.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            
            # Atomic write
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(session.dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            
            temp_path.replace(path)
    
    async def load_session(self, session_id: str) -> Session:
        """Load session with lock."""
        async with self._session_locks[session_id]:
            # Read session
            path = self._get_session_path(session_id)
            if not path.exists():
                raise SessionNotFoundError(session_id)
            
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return Session(**data)
```

---

### 3.4 Fix Type Safety

**Priority:** 🟢 P2

#### Fix 1: Add Type Hints to Core Functions

**Files:**
- `src/agent_system/services/agent_service.py`
- `src/agent_system/services/tool_service.py`
- `src/agent_system/services/session_service.py`

**Example:**
```python
# Before
def execute_task(task, session_id=None, images=None):
    ...

# After
async def execute_task(
    self,
    task: str,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    images: Optional[list[bytes]] = None
) -> AsyncIterator[dict[str, Any]]:
    """Execute an agent task with streaming results.
    
    Args:
        task: Task description or prompt.
        session_id: Optional session ID for multi-turn conversations.
        request_id: Optional request ID for tracking.
        images: Optional list of image bytes for multimodal input.
    
    Yields:
        Event dictionaries with structure:
        - type: Event type (step, thought, tool_call, result, error, end)
        - data: Event-specific data
        - timestamp: ISO timestamp
    
    Raises:
        ValidationError: If task is empty or invalid.
        LLMConnectionError: If LLM service is unavailable.
    """
    ...
```

#### Fix 2: Enable Mypy Strict Mode

**Update:** `pyproject.toml`

```toml
[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_any_generics = false  # Too strict for now
check_untyped_defs = true

# Per-module overrides (gradual adoption)
[[tool.mypy.overrides]]
module = "plugins.*"
ignore_errors = true  # Fix plugins later

[[tool.mypy.overrides]]
module = "tests.*"
ignore_errors = true  # Fix tests later
```

---

### 3.5 Fix Logging Issues

**Priority:** 🟢 P2

#### Fix 1: Standardize Log Levels

**Guidelines:**
```python
# DEBUG: Detailed debugging info (not in production)
logger.debug("Processing message: %s", message[:100])

# INFO: Normal operations
logger.info("Task started: request_id=%s", request_id)

# WARNING: Unexpected but handled
logger.warning("Retrying LLM call after rate limit: attempt=%d", retry)

# ERROR: Operation failed but app continues
logger.error("Failed to save session: session_id=%s, error=%s", session_id, e)

# CRITICAL: App cannot continue
logger.critical("Cannot load config file: %s", config_path)
```

#### Fix 2: Structured Logging

**Create:** `src/agent_system/utils/structured_log.py`

```python
"""Structured logging helpers."""
import logging
from typing import Any

class StructuredLogger:
    """Logger that adds structured context."""
    
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
    
    def info(self, message: str, **context):
        """Log info with context."""
        self.logger.info(message, extra={"context": context})
    
    def error(self, message: str, exc: Exception = None, **context):
        """Log error with context."""
        if exc:
            context["exception_type"] = type(exc).__name__
            context["exception_message"] = str(exc)
        self.logger.error(message, extra={"context": context}, exc_info=exc)

# Usage
logger = StructuredLogger(__name__)

logger.info(
    "Task executed",
    request_id=request_id,
    session_id=session_id,
    duration_ms=duration * 1000
)

logger.error(
    "Failed to execute task",
    exc=e,
    request_id=request_id,
    session_id=session_id
)
```

---

## 4. Code Quality Improvements

### 4.1 Remove Code Duplication

**Priority:** 🟢 P2

#### Issue: Repeated Exception Handling

**Found in:** `src/agent_system/api/session_endpoints.py`

**Before (repeated 5+ times):**
```python
@router.get("/sessions")
async def list_sessions(...):
    try:
        # ... logic ...
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except SessionPermissionError:
        raise HTTPException(status_code=403, detail="Access denied")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sessions/{session_id}")
async def get_session(...):
    try:
        # ... logic ...
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except SessionPermissionError:
        raise HTTPException(status_code=403, detail="Access denied")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

**After (DRY with decorator):**
```python
from functools import wraps

def handle_session_errors(func):
    """Decorator to handle common session errors."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except SessionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except SessionPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return wrapper

# Usage
@router.get("/sessions")
@handle_session_errors
async def list_sessions(...):
    # No try/except needed!
    sessions = await session_service.list_sessions(...)
    return sessions
```

---

### 4.2 Add Input Validation

**Priority:** 🟢 P2

#### Add Validation to API Endpoints

**Example:**
```python
from pydantic import BaseModel, Field, validator

class ChatRequest(BaseModel):
    """Chat request validation."""
    message: str = Field(..., min_length=1, max_length=10000)
    session_id: Optional[str] = Field(None, regex="^sess_[a-zA-Z0-9]{8}$")
    agent_name: str = Field("default", min_length=1, max_length=50)
    images: Optional[list[str]] = Field(None, max_items=10)  # Base64
    
    @validator('message')
    def message_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Message cannot be empty or whitespace only")
        return v.strip()
    
    @validator('images')
    def validate_images(cls, v):
        if v:
            for i, img in enumerate(v):
                if len(img) > 10 * 1024 * 1024:  # 10MB
                    raise ValueError(f"Image {i} too large (max 10MB)")
        return v

# Usage
@router.post("/chat")
async def chat(request: ChatRequest):  # Automatic validation!
    ...
```

---

## 5. Error Handling Improvements

### 5.1 Add Retry Logic

**For:** LLM API calls, external services

**Create:** `src/agent_system/utils/retry.py`

```python
"""Retry utilities with exponential backoff."""
import asyncio
import logging
from typing import Callable, TypeVar, Optional
from functools import wraps

logger = logging.getLogger(__name__)

T = TypeVar('T')

async def retry_with_backoff(
    func: Callable[..., T],
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,)
) -> T:
    """Retry function with exponential backoff.
    
    Args:
        func: Async function to retry.
        max_retries: Maximum retry attempts.
        initial_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        exponential_base: Base for exponential backoff.
        exceptions: Tuple of exceptions to retry on.
    
    Returns:
        Function result.
    
    Raises:
        Last exception if all retries fail.
    """
    delay = initial_delay
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            
            if attempt == max_retries:
                logger.error(
                    "All retries exhausted for %s: %s",
                    func.__name__,
                    e
                )
                raise
            
            logger.warning(
                "Retry %d/%d for %s after error: %s",
                attempt + 1,
                max_retries,
                func.__name__,
                e
            )
            
            await asyncio.sleep(delay)
            delay = min(delay * exponential_base, max_delay)
    
    raise last_exception

# Decorator version
def with_retry(
    max_retries: int = 3,
    exceptions: tuple = (Exception,)
):
    """Decorator for retry with backoff."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_with_backoff(
                lambda: func(*args, **kwargs),
                max_retries=max_retries,
                exceptions=exceptions
            )
        return wrapper
    return decorator

# Usage
@with_retry(max_retries=3, exceptions=(LLMConnectionError,))
async def call_llm(prompt: str):
    """Call LLM with automatic retry."""
    response = await llm_client.chat(prompt)
    return response
```

---

### 5.2 Add Circuit Breaker

**For:** Prevent cascading failures

**Create:** `src/agent_system/utils/circuit_breaker.py`

```python
"""Circuit breaker pattern for fault tolerance."""
import asyncio
import time
from enum import Enum
from typing import Callable, TypeVar
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered

class CircuitBreaker:
    """Circuit breaker for external service calls."""
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: type = Exception
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = CircuitState.CLOSED
    
    async def call(self, func: Callable[..., T]) -> T:
        """Execute function with circuit breaker."""
        
        # Check if circuit is open
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                logger.info("Circuit breaker entering half-open state")
                self.state = CircuitState.HALF_OPEN
            else:
                raise Exception("Circuit breaker is OPEN")
        
        try:
            result = await func()
            
            # Success - reset failure count
            if self.state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker closing after successful call")
                self.state = CircuitState.CLOSED
            
            self.failure_count = 0
            return result
        
        except self.expected_exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            logger.warning(
                "Circuit breaker failure %d/%d: %s",
                self.failure_count,
                self.failure_threshold,
                e
            )
            
            # Open circuit if threshold reached
            if self.failure_count >= self.failure_threshold:
                logger.error("Circuit breaker opening after %d failures", self.failure_count)
                self.state = CircuitState.OPEN
            
            raise

# Usage
llm_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=LLMConnectionError
)

async def call_llm_with_breaker(prompt: str):
    """Call LLM with circuit breaker protection."""
    return await llm_circuit_breaker.call(
        lambda: llm_client.chat(prompt)
    )
```

---

## 6. Performance Optimizations

### 6.1 Session Loading Optimization

**Issue:** Loading entire session history every time

**Fix:** Lazy loading + pagination

```python
class Session:
    """Session with lazy message loading."""
    
    def __init__(self, id: str, username: str):
        self.id = id
        self.username = username
        self._messages: Optional[list[ChatMessage]] = None
    
    async def get_messages(
        self,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> list[ChatMessage]:
        """Get messages with pagination."""
        if self._messages is None:
            # Load all messages (TODO: load from database)
            self._messages = await self._load_messages()
        
        # Return slice
        if limit:
            return self._messages[offset:offset + limit]
        return self._messages[offset:]
    
    async def get_recent_messages(self, count: int = 10) -> list[ChatMessage]:
        """Get N most recent messages."""
        if self._messages is None:
            # Optimize: only load recent messages from file
            self._messages = await self._load_recent_messages(count)
        
        return self._messages[-count:]
```

---

### 6.2 Tool Result Caching

**Issue:** Same tool calls repeated

**Fix:** LRU cache for deterministic tools

```python
from functools import lru_cache
import hashlib
import json

class ToolService:
    """Tool service with result caching."""
    
    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._cache_max_size = 1000
    
    def _cache_key(self, tool_name: str, args: dict) -> str:
        """Generate cache key for tool call."""
        # Hash arguments for consistent key
        args_json = json.dumps(args, sort_keys=True)
        args_hash = hashlib.sha256(args_json.encode()).hexdigest()
        return f"{tool_name}:{args_hash}"
    
    async def execute_tool(
        self,
        tool_name: str,
        args: dict,
        use_cache: bool = True
    ) -> Any:
        """Execute tool with optional caching."""
        
        # Check if tool is cacheable
        tool = self.get_tool(tool_name)
        if not tool.cacheable:
            use_cache = False
        
        # Check cache
        if use_cache:
            cache_key = self._cache_key(tool_name, args)
            if cache_key in self._cache:
                logger.debug("Tool cache hit: %s", tool_name)
                return self._cache[cache_key]
        
        # Execute tool
        result = await tool.execute(**args)
        
        # Store in cache
        if use_cache:
            cache_key = self._cache_key(tool_name, args)
            self._cache[cache_key] = result
            
            # Evict oldest if cache full
            if len(self._cache) > self._cache_max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
        
        return result
```

---

## 7. Implementation Plan

### Week 1: Critical Fixes (P0)

| Day | Task | Deliverable |
|-----|------|-------------|
| Mon | Create `exceptions.py` | Custom exception classes |
| Tue | Fix exception handling in API | Proper error types |
| Tue | Add global exception handlers | Centralized error handling |
| Wed | Fix file handle leaks | Use context managers |
| Wed | Fix async task cleanup | Proper shutdown |
| Thu | Add file locking (backlog) | `SafeFileWriter` class |
| Fri | Add session locking | Per-session locks |

**Testing:**
```bash
# Run tests
.venv/Scripts/python.exe -m pytest tests/ -v

# Check for resource leaks
# (run server, hit endpoints, check open files)
```

---

### Week 2: Quality & Performance (P1-P2)

| Day | Task | Deliverable |
|-----|------|-------------|
| Mon | Add type hints | Better IDE support |
| Mon | Enable mypy checks | Type safety |
| Tue | Add retry logic | `retry.py` module |
| Tue | Add circuit breaker | `circuit_breaker.py` |
| Wed | Optimize session loading | Lazy loading |
| Wed | Add tool caching | LRU cache |
| Thu | Standardize logging | Log level guidelines |
| Thu | Add structured logging | Context in logs |
| Fri | Remove code duplication | DRY refactoring |

---

## 8. Testing Strategy

### 8.1 Test Exception Handling

```python
# tests/test_exceptions.py
import pytest
from agent_system.exceptions import SessionNotFoundError

def test_session_not_found_exception():
    """Test SessionNotFoundError."""
    exc = SessionNotFoundError("sess_12345678")
    assert exc.session_id == "sess_12345678"
    assert "sess_12345678" in str(exc)

@pytest.mark.asyncio
async def test_api_exception_handling(client):
    """Test API returns correct error codes."""
    response = await client.get("/api/v1/sessions/nonexistent")
    assert response.status_code == 404
    assert response.json()["error"] == "SessionNotFoundError"
```

---

### 8.2 Test File Locking

```python
# tests/test_file_locking.py
import asyncio
from pathlib import Path
from agent_system.utils.file_lock import SafeFileWriter

def test_concurrent_file_writes(tmp_path):
    """Test concurrent writes don't corrupt file."""
    file_path = tmp_path / "test.txt"
    writer = SafeFileWriter(file_path)
    
    # Simulate 10 concurrent writes
    async def write_number(n):
        writer.write(f"Line {n}\n")
    
    # All writes should succeed
    asyncio.run(asyncio.gather(*[
        write_number(i) for i in range(10)
    ]))
    
    # File should contain all lines
    content = file_path.read_text()
    assert content.count("Line") == 10
```

---

### 8.3 Test Retry Logic

```python
# tests/test_retry.py
import pytest
from agent_system.utils.retry import retry_with_backoff

@pytest.mark.asyncio
async def test_retry_succeeds_after_failures():
    """Test retry logic works."""
    call_count = 0
    
    async def flaky_function():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception("Temporary failure")
        return "Success"
    
    result = await retry_with_backoff(flaky_function, max_retries=5)
    assert result == "Success"
    assert call_count == 3  # Failed twice, succeeded third time
```

---

## 9. Success Criteria

### 9.1 Bug Metrics

| Metric | Before | Target |
|--------|--------|--------|
| **Unhandled Exceptions** | ~5/day | <1/week |
| **File Corruption Issues** | ~1/week | 0 |
| **Resource Leaks** | Unknown | 0 |
| **Type Errors** | ~10 | 0 |
| **Test Coverage** | ~70% | >85% |

---

### 9.2 Code Quality Metrics

| Metric | Before | Target |
|--------|--------|--------|
| **Bare except:** blocks | 15+ | 0 |
| **Functions with type hints** | ~50% | >90% |
| **Duplicate code blocks** | 10+ | <3 |
| **mypy errors** | 100+ | <10 |
| **pylint score** | 7/10 | >9/10 |

---

## 10. Rollout Plan

### 10.1 Gradual Rollout

```
Week 1 (P0 fixes)
    │
    ├─► Deploy to dev
    ├─► Run integration tests
    ├─► Monitor for issues
    │
    └─► Deploy to production (Friday)

Week 2 (P1-P2 improvements)
    │
    ├─► Deploy to dev
    ├─► Performance testing
    ├─► Code review
    │
    └─► Deploy to production (Friday)
```

---

### 10.2 Rollback Plan

**If critical issues found:**

```bash
# Git tag before changes
git tag v1.0-stable

# If rollback needed
git revert <commit-range>
# OR
git reset --hard v1.0-stable
```

---

## 11. Conclusion

### v1.1 Delivers:

- ✅ **Stability** - Proper error handling, no crashes
- ✅ **Reliability** - File locking, no data loss
- ✅ **Maintainability** - Type hints, clean code
- ✅ **Performance** - Caching, optimizations
- ✅ **Production-Ready** - Retry logic, circuit breakers

### Next Steps:

1. **Week 1:** Fix critical bugs (P0)
2. **Week 2:** Improve code quality (P1-P2)
3. **After v1.1:** Move to v1.5 (hot-reload, metrics)

---

**Ready to implement! Let's make the code rock-solid. 🎯**

---

**Prepared by:** AgentSystem Architecture Team  
**Date:** 2025-10-15  
**Status:** ✅ READY FOR IMPLEMENTATION
