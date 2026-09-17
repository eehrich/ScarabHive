"""Extended security tests for script interpreter - comprehensive host protection."""

import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.config.models import AgentSystemConfig, ToolServerConfig

from src.plugins.script_interpreter.server import ScriptInterpreterServer


class MockStatus:
    """Mock status object for testing."""
    def __init__(self):
        self.progress = AsyncMock()
        self.update = AsyncMock()
        self.set_error = AsyncMock()
        self.set_success = AsyncMock()
        self.error = AsyncMock()
        self.end = AsyncMock()


@pytest.fixture
async def server():
    """Create a script interpreter server for testing."""
    system_config = Mock(spec=AgentSystemConfig)
    server_config = ToolServerConfig(type="script_interpreter", enabled=True)
    server = ScriptInterpreterServer("script_interpreter", system_config, server_config)
    yield server


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_file_system_access_blocked(server, mock_status):
    """Test that file system access is completely blocked."""
    dangerous_file_operations = [
        "open('/etc/passwd', 'r')",
        "open('C:\\\\Windows\\\\System32\\\\config\\\\SAM', 'r')",
        "open('test.txt', 'w').write('malicious')",
        "with open('temp.txt', 'w') as f: f.write('test')",
        "file('test.txt', 'w')",
    ]
    
    for code in dangerous_file_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"File operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_environment_variable_access_blocked(server, mock_status):
    """Test that environment variable access is blocked."""
    env_access_attempts = [
        "import os; os.environ['PATH']",
        "import os; os.getenv('HOME')",
        "import os; os.putenv('MALICIOUS', 'value')",
        "import os; del os.environ['PATH']",
    ]
    
    for code in env_access_attempts:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Environment access should be blocked: {code}"


@pytest.mark.asyncio
async def test_process_manipulation_blocked(server, mock_status):
    """Test that process manipulation is completely blocked."""
    process_operations = [
        "import subprocess; subprocess.call(['ls'])",
        "import subprocess; subprocess.Popen(['cmd', '/c', 'dir'])",
        "import os; os.system('echo malicious')",
        "import os; os.popen('whoami').read()",
        "import os; os.fork()",
        "import os; os.execv('/bin/sh', [])",
        "import os; os.spawnl(os.P_WAIT, '/bin/ls')",
    ]
    
    for code in process_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Process operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_network_access_blocked(server, mock_status):
    """Test that network access is completely blocked."""
    network_operations = [
        "import socket; socket.socket()",
        "import urllib.request; urllib.request.urlopen('http://example.com')",
        "import http.client; http.client.HTTPConnection('example.com')",
        "import ftplib; ftplib.FTP('ftp.example.com')",
        "import smtplib; smtplib.SMTP('localhost')",
        "import telnetlib; telnetlib.Telnet('localhost')",
    ]
    
    for code in network_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Network operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_reflection_and_introspection_blocked(server, mock_status):
    """Test that dangerous reflection operations are blocked."""
    reflection_operations = [
        "globals()",
        "locals()",
        "vars()",
        "dir()",
        "__import__('os')",
        "getattr(__builtins__, 'open')",
        "setattr(__builtins__, 'evil', lambda: None)",
        "delattr(__builtins__, 'print')",
        "hasattr(__builtins__, '__import__')",
        "eval('__import__(\"os\")')",
        "exec('import os')",
        "compile('import os', '<string>', 'exec')",
    ]
    
    for code in reflection_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Reflection operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_memory_and_gc_manipulation_blocked(server, mock_status):
    """Test that memory and garbage collector manipulation is blocked."""
    memory_operations = [
        "import gc; gc.disable()",
        "import gc; gc.collect()",
        "import ctypes; ctypes.c_int()",
        "import sys; sys.exit()",
        "import sys; sys.settrace(lambda *args: None)",
        "import sys; sys.setprofile(lambda *args: None)",
    ]
    
    for code in memory_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Memory operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_module_loading_manipulation_blocked(server, mock_status):
    """Test that module loading manipulation is blocked."""
    module_operations = [
        "import importlib; importlib.import_module('os')",
        "import importlib.util; importlib.util.find_spec('os')",
        "import sys; sys.modules['os'] = None",
        "import sys; del sys.modules['math']",
        "__import__('subprocess')",
    ]
    
    for code in module_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Module operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_class_and_object_manipulation_blocked(server, mock_status):
    """Test that dangerous class and object manipulation is blocked."""
    class_operations = [
        "class Evil: pass; Evil.__dict__",
        "type.__subclasses__()",
        "object.__subclasses__()",
        "().__class__.__bases__[0].__subclasses__()",
        "''.__class__.__mro__[2].__subclasses__()",
    ]
    
    for code in class_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Class manipulation should be blocked: {code}"


@pytest.mark.asyncio
async def test_encoding_and_codec_manipulation_blocked(server, mock_status):
    """Test that encoding and codec manipulation is blocked."""
    codec_operations = [
        "import codecs; codecs.open('test.txt', 'w')",
        "import base64; base64.b64decode",
        "import binascii; binascii.unhexlify",
        "import zlib; zlib.decompress",
        "import gzip; gzip.open",
    ]
    
    for code in codec_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Codec operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_time_and_signal_manipulation_blocked(server, mock_status):
    """Test that time and signal manipulation is blocked."""
    time_operations = [
        "import signal; signal.signal(signal.SIGINT, lambda x,y: None)",
        "import signal; signal.alarm(1)",
        "import time; time.sleep(100)",  # Long sleep should be handled by timeout
        "import threading; threading.Thread(target=lambda: None).start()",
        "import multiprocessing; multiprocessing.Process().start()",
    ]
    
    for code in time_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Time/signal operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_serialization_and_persistence_blocked(server, mock_status):
    """Test that serialization and persistence operations are blocked."""
    serialization_operations = [
        "import pickle; pickle.dumps({'key': 'value'})",
        "import pickle; pickle.loads(b'\\x80\\x03}')",
        "import shelve; shelve.open('test.db')",
        "import dbm; dbm.open('test.db', 'c')",
        "import sqlite3; sqlite3.connect(':memory:')",
    ]
    
    for code in serialization_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Serialization operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_logging_and_debugging_blocked(server, mock_status):
    """Test that logging and debugging operations are blocked."""
    debug_operations = [
        "import logging; logging.getLogger().addHandler(logging.FileHandler('test.log'))",
        "import pdb; pdb.set_trace()",
        "import trace; trace.Trace().run('print(1)')",
        "import cProfile; cProfile.run('print(1)')",
    ]
    
    for code in debug_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Debug operation should be blocked: {code}"


@pytest.mark.asyncio
async def test_legitimate_operations_still_work(server, mock_status):
    """Test that legitimate operations are not blocked."""
    safe_operations = [
        "x = 1 + 2",
        "print('hello world')",
        "[1, 2, 3].append(4)",
        "{'a': 1, 'b': 2}.keys()",
        "sqrt(16)",  # Math functions are available directly
        "sum([1, 2, 3, 4])",
        "max([1, 5, 3])",
        "len('hello')",
        "'hello'.upper()",
        "list(range(10))",
    ]
    
    for code in safe_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" not in result, f"Safe operation should work: {code}"
        assert "result" in result


@pytest.mark.asyncio
async def test_code_injection_attempts_blocked(server, mock_status):
    """Test various code injection attempts are blocked."""
    injection_attempts = [
        # String-based injection attempts
        "eval('__import__(\"os\").system(\"echo pwned\")')",
        "exec('import os; os.system(\"ls\")')",
        
        # Dynamic attribute access
        "getattr(__builtins__, '__import__')('os')",
        
        # Bytecode manipulation attempts
        "compile('import os', '<string>', 'exec')",
        
        # Function creation attempts
        "type(lambda: None)(__import__('os').system, (), {})()",
        
        # Memory address manipulation
        "id(__import__)",
        
        # Stack frame manipulation
        "import inspect; inspect.currentframe()",
    ]
    
    for code in injection_attempts:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Code injection should be blocked: {code}"


@pytest.mark.asyncio
async def test_resource_exhaustion_protection(server, mock_status):
    """Test protection against resource exhaustion attacks."""
    resource_exhaustion_attempts = [
        # CPU exhaustion (should be caught by timeout)
        "while True: pass",
        "for i in range(10**8): pass",
        
        # Recursive exhaustion
        "def f(): f()\nf()",
    ]
    
    for code in resource_exhaustion_attempts:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" in result, f"Resource exhaustion should be blocked: {code}"


@pytest.mark.asyncio 
async def test_memory_usage_reasonable(server, mock_status):
    """Test that reasonable memory usage is allowed but extreme usage might be limited."""
    # Moderate memory usage should work
    moderate_operations = [
        "x = 'a' * 1000",
        "y = [0] * 1000", 
        "z = {'key' + str(i): i for i in range(100)}",
    ]
    
    for code in moderate_operations:
        result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
        assert "error" not in result, f"Moderate memory usage should work: {code}"