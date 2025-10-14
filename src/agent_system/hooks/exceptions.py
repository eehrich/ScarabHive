"""Hook system exception classes."""


class HookError(Exception):
    """Base exception for all hook-related errors."""
    pass


class HookOrderingError(HookError):
    """Error in hook ordering specification."""
    pass


class CircularDependencyError(HookOrderingError):
    """Circular dependency detected in hook ordering."""
    pass


class HookExecutionError(HookError):
    """Error during hook execution."""
    pass


class HookTimeoutError(HookExecutionError):
    """Hook execution exceeded timeout limit."""
    pass
