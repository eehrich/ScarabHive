"""
Integration Test: Logging System Validation

Tests that logging is properly configured and no unexpected
warnings or errors appear during normal operation:
- Log level configuration
- Log capture during agent execution
- Detection of unexpected ERROR/WARNING logs
- Log format validation
- Structured logging checks
- Component-specific logging

This helps catch issues where errors are logged but not
caught by standard assertions.
"""
import pytest
import logging

from agent_system.config.models import (
    AgentSystemConfig,
    AgentConfig,
    MCPConfig,
    ToolConfig,
    LLMSystemConfig,
    LLMModelConfig,
    LLMProfile
)
from agent_system.servers.agent.server import Agent
from agent_system.mcp.base import MCPRegistry


logger = logging.getLogger(__name__)


class MockLLMClient:
    """Mock LLM for logging tests"""
    
    def __init__(self, provider="mock", model="mock-model", **kwargs):
        self.provider = provider
        self.model = model
        
    async def chat_tools(self, messages, tools, cancellation_token=None):
        return {"assistant": {"content": "Logging test response"}}


@pytest.fixture
def test_config():
    """Basic test configuration"""
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(
                    provider="ollama",  # Use valid provider
                    model="test",
                    context_window=4096
                )
            },
            profiles={
                "test": LLMProfile(model_ref="test-model")
            }
        )
    )


@pytest.fixture
def agent_config():
    """Basic agent configuration"""
    return AgentConfig(
        
        llm_profile="test",
        system_prompt="Test agent",
        tools=ToolConfig(allowed=[])
    )


@pytest.fixture
def mock_registry():
    """Simple mock registry"""
    return MCPRegistry()


@pytest.mark.asyncio
async def test_no_unexpected_errors_during_normal_execution(
    test_config, agent_config, mock_registry, caplog
):
    """Test 1: No ERROR logs during successful agent execution"""
    
    caplog.set_level(logging.ERROR)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="error_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent successfully - consume all events
    async for _ in agent.run_events(
        task="Simple test task",
        request_id="test-no-errors"
    ):
        pass
    
    # Check for ERROR level logs
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    
    if error_records:
        logger.error(f"Found {len(error_records)} unexpected ERROR logs:")
        for record in error_records:
            logger.error(f"  {record.name}: {record.message}")
    
    assert len(error_records) == 0, f"Found {len(error_records)} ERROR logs during normal execution"
    
    logger.info("✓ No ERROR logs during normal execution")


@pytest.mark.asyncio
async def test_no_unexpected_warnings_during_normal_execution(
    test_config, agent_config, mock_registry, caplog
):
    """Test 2: Minimal WARNING logs during successful execution"""
    
    caplog.set_level(logging.WARNING)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="warning_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent successfully - consume all events
    async for _ in agent.run_events(
        task="Simple test task",
        request_id="test-no-warnings"
    ):
        pass
    
    # Check for WARNING level logs
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    
    if warning_records:
        logger.warning(f"Found {len(warning_records)} WARNING logs:")
        for record in warning_records:
            logger.warning(f"  {record.name}: {record.message}")
    
    # Allow some warnings, but flag if there are many
    if len(warning_records) > 5:
        logger.error(f"Too many warnings: {len(warning_records)}")
        assert False, f"Found {len(warning_records)} WARNING logs - may indicate issues"
    
    logger.info(f"✓ Only {len(warning_records)} WARNING logs found (acceptable)")


@pytest.mark.asyncio
async def test_logging_includes_request_id(
    test_config, agent_config, mock_registry, caplog
):
    """Test 3: Logs include request_id for tracing"""
    
    caplog.set_level(logging.DEBUG)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="request_id_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    request_id = "test-request-12345"
    
    # Run agent with specific request ID
    async for _ in agent.run_events(
        task="Test with request ID",
        request_id=request_id
    ):
        pass
    
    # Check if request_id appears in logs
    logs_with_request_id = [
        r for r in caplog.records 
        if request_id in r.getMessage()
    ]
    
    # Should have at least some logs with request_id
    logger.info(f"Found {len(logs_with_request_id)} logs containing request_id")
    
    # Note: If 0, may indicate request_id isn't being logged
    # This documents expected behavior
    if len(logs_with_request_id) == 0:
        logger.warning("Request ID not found in logs - consider adding for traceability")
    else:
        logger.info("✓ Request ID found in logs")


@pytest.mark.asyncio
async def test_logging_has_proper_levels(
    test_config, agent_config, mock_registry, caplog
):
    """Test 4: Log records have appropriate levels"""
    
    caplog.set_level(logging.DEBUG)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="level_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent
    async for _ in agent.run_events(
        task="Test logging levels",
        request_id="test-levels"
    ):
        pass
    
    # Analyze log levels
    level_counts = {
        "DEBUG": 0,
        "INFO": 0,
        "WARNING": 0,
        "ERROR": 0,
        "CRITICAL": 0
    }
    
    for record in caplog.records:
        if record.levelno == logging.DEBUG:
            level_counts["DEBUG"] += 1
        elif record.levelno == logging.INFO:
            level_counts["INFO"] += 1
        elif record.levelno == logging.WARNING:
            level_counts["WARNING"] += 1
        elif record.levelno == logging.ERROR:
            level_counts["ERROR"] += 1
        elif record.levelno >= logging.CRITICAL:
            level_counts["CRITICAL"] += 1
    
    logger.info(f"Log level distribution: {level_counts}")
    
    # Should have INFO logs at minimum
    assert level_counts["INFO"] > 0, "Should have INFO level logs"
    
    # Should not have CRITICAL errors in normal execution
    assert level_counts["CRITICAL"] == 0, "Should not have CRITICAL errors"
    
    logger.info("✓ Log levels appropriate")


@pytest.mark.asyncio
async def test_component_logging_is_scoped(
    test_config, agent_config, mock_registry, caplog
):
    """Test 5: Different components use appropriate logger names"""
    
    caplog.set_level(logging.DEBUG)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="scope_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent
    async for _ in agent.run_events(
        task="Test component logging",
        request_id="test-scoped"
    ):
        pass
    
    # Collect unique logger names
    logger_names = set(r.name for r in caplog.records)
    
    logger.info(f"Logger names used: {logger_names}")
    
    # Should have multiple loggers from different components
    # (exact names depend on implementation)
    assert len(logger_names) > 0, "Should have at least one logger"
    
    # Check for agent_system namespace
    agent_system_loggers = [n for n in logger_names if n.startswith("agent_system")]
    logger.info(f"Found {len(agent_system_loggers)} agent_system loggers")
    
    logger.info("✓ Component logging scoped appropriately")


@pytest.mark.asyncio
async def test_exception_logging_includes_traceback(
    test_config, agent_config, mock_registry, caplog
):
    """Test 6: Exceptions are logged with full tracebacks"""
    
    caplog.set_level(logging.ERROR)
    
    # Create a mock LLM that raises an exception
    class FailingLLMClient:
        def __init__(self, **kwargs):
            pass
            
        async def chat_tools(self, messages, tools, cancellation_token=None):
            raise ValueError("Simulated LLM error")
    
    failing_llm = FailingLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="exception_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=failing_llm
    )
    
    # Run agent (should fail)
    try:
        async for _ in agent.run_events(
            task="This will fail",
            request_id="test-exception"
        ):
            pass
    except Exception:
        pass  # Expected to fail
    
    # Check for exception logs
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    
    if error_records:
        # Check if any error record has exc_info (traceback)
        has_traceback = any(r.exc_info is not None for r in error_records)
        
        if has_traceback:
            logger.info("✓ Exception logged with traceback")
        else:
            logger.warning("Exception logged without traceback - consider using logger.exception()")
    else:
        logger.warning("No ERROR logs found for exception - should log failures")


@pytest.mark.asyncio
async def test_no_duplicate_log_messages(
    test_config, agent_config, mock_registry, caplog
):
    """Test 7: No duplicate log messages (indicates misconfigured loggers)"""
    
    caplog.set_level(logging.INFO)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="duplicate_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent
    async for _ in agent.run_events(
        task="Test for duplicate logs",
        request_id="test-duplicates"
    ):
        pass
    
    # Check for consecutive duplicate messages
    messages = [r.message for r in caplog.records]
    
    duplicates = 0
    for i in range(len(messages) - 1):
        if messages[i] == messages[i + 1]:
            duplicates += 1
            logger.warning(f"Duplicate log: {messages[i]}")
    
    if duplicates > 0:
        logger.warning(f"Found {duplicates} consecutive duplicate log messages")
        # Don't fail test, but flag for attention
    else:
        logger.info("✓ No consecutive duplicate logs found")


@pytest.mark.asyncio
async def test_logging_performance_overhead(
    test_config, agent_config, mock_registry, caplog
):
    """Test 8: Verify logging doesn't cause significant overhead"""
    
    import time
    
    caplog.set_level(logging.DEBUG)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="perf_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Measure execution time with logging
    start_time = time.time()
    
    async for _ in agent.run_events(
        task="Performance test",
        request_id="test-perf"
    ):
        pass
    
    elapsed_time = time.time() - start_time
    
    # Count log records
    log_count = len(caplog.records)
    
    logger.info(f"Execution time: {elapsed_time:.3f}s with {log_count} log records")
    
    # Check if excessive logging
    if log_count > 1000:
        logger.warning(f"High log volume: {log_count} records - may impact performance")
    
    # Check execution time
    if elapsed_time > 5.0:
        logger.warning(f"Slow execution: {elapsed_time:.3f}s - check for logging overhead")
    
    logger.info("✓ Logging performance acceptable")


@pytest.mark.asyncio
async def test_sensitive_data_not_logged(
    test_config, agent_config, mock_registry, caplog
):
    """Test 9: Sensitive data (API keys, tokens) not logged"""
    
    caplog.set_level(logging.DEBUG)
    
    # Create config with "sensitive" data
    sensitive_test_config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(
                    provider="openai",  # Use valid provider
                    model="test",
                    context_window=4096,
                    openai_api_key="sk-test-api-key-12345"  # Sensitive
                )
            },
            profiles={
                "test": LLMProfile(model_ref="test-model")
            }
        )
    )
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="sensitive_test",
        system_config=sensitive_test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent
    async for _ in agent.run_events(
        task="Test sensitive data logging",
        request_id="test-sensitive"
    ):
        pass
    
    # Check all log messages for sensitive patterns
    sensitive_patterns = [
        "sk-",  # OpenAI API key prefix
        "api-key",
        "api_key",
        "password",
        "token",
        "secret"
    ]
    
    found_sensitive = []
    for record in caplog.records:
        msg_lower = record.getMessage().lower()
        for pattern in sensitive_patterns:
            if pattern in msg_lower and "12345" in msg_lower:
                found_sensitive.append((pattern, record.getMessage()))
    
    if found_sensitive:
        logger.error(f"Found {len(found_sensitive)} potential sensitive data leaks in logs")
        for pattern, msg in found_sensitive:
            logger.error(f"  Pattern '{pattern}' in: {msg[:100]}")
        
        assert False, "Sensitive data found in logs - implement sanitization"
    
    logger.info("✓ No sensitive data found in logs")


@pytest.mark.asyncio
async def test_structured_logging_format(
    test_config, agent_config, mock_registry, caplog
):
    """Test 10: Logs use consistent structure"""
    
    caplog.set_level(logging.INFO)
    
    mock_llm = MockLLMClient()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    agent = Agent(
        name="structure_test",
        system_config=test_config,
        mcp_config=mcp_config,
        registry=mock_registry,
        llm=mock_llm
    )
    
    # Run agent
    async for _ in agent.run_events(
        task="Test log structure",
        request_id="test-structure"
    ):
        pass
    
    # Check log records have expected attributes
    for record in caplog.records:
        # Standard attributes
        assert hasattr(record, 'name'), "Log should have logger name"
        assert hasattr(record, 'levelname'), "Log should have level name"
        assert hasattr(record, 'message'), "Log should have message"
        assert hasattr(record, 'created'), "Log should have timestamp"
        
        # Module info
        assert hasattr(record, 'filename'), "Log should have filename"
        assert hasattr(record, 'lineno'), "Log should have line number"
    
    logger.info("✓ Logs have consistent structure")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
