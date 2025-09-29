"""
Tests for string methods in the script interpreter plugin.
"""

import pytest
from src.plugins.script_interpreter.server import ScriptInterpreterServer


class MockStatus:
    """Mock status for testing."""
    def __init__(self):
        self.messages = []
    
    async def progress(self, msg):
        self.messages.append(("progress", msg))
    
    async def error(self, msg):
        self.messages.append(("error", msg))
    
    async def end(self, msg, meta=None):
        self.messages.append(("end", msg, meta))


@pytest.fixture
def server():
    """Create script interpreter server for testing."""
    return ScriptInterpreterServer("test", {})


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_basic_string_methods(server, mock_status):
    """Test basic string transformation methods."""
    code = """
text = "Hello World"
upper_text = text.upper()
lower_text = text.lower()
title_text = text.title()
print(f"Upper: {upper_text}")
print(f"Lower: {lower_text}")
print(f"Title: {title_text}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Upper: HELLO WORLD" in result["result"]
    assert "Lower: hello world" in result["result"]
    assert "Title: Hello World" in result["result"]


@pytest.mark.asyncio
async def test_string_stripping_methods(server, mock_status):
    """Test string stripping methods."""
    code = """
text = "   Hello World   "
stripped = text.strip()
lstripped = text.lstrip()
rstripped = text.rstrip()
print(f"Original length: {len(text)}")
print(f"Stripped: '{stripped}'")
print(f"Left stripped: '{lstripped}'")
print(f"Right stripped: '{rstripped}'")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Original length: 17" in result["result"]
    assert "Stripped: 'Hello World'" in result["result"]
    assert "Left stripped: 'Hello World   '" in result["result"]
    assert "Right stripped: '   Hello World'" in result["result"]


@pytest.mark.asyncio
async def test_string_search_methods(server, mock_status):
    """Test string search and find methods."""
    code = """
text = "Hello World Hello"
find_hello = text.find('Hello')
find_world = text.find('World')
find_missing = text.find('Python')
count_hello = text.count('Hello')
print(f"Find Hello: {find_hello}")
print(f"Find World: {find_world}")
print(f"Find Python: {find_missing}")
print(f"Count Hello: {count_hello}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Find Hello: 0" in result["result"]
    assert "Find World: 6" in result["result"]
    assert "Find Python: -1" in result["result"]
    assert "Count Hello: 2" in result["result"]


@pytest.mark.asyncio
async def test_string_startswith_endswith(server, mock_status):
    """Test string prefix and suffix checking."""
    code = """
text = "Hello World"
starts_hello = text.startswith('Hello')
starts_world = text.startswith('World')
ends_world = text.endswith('World')
ends_hello = text.endswith('Hello')
print(f"Starts with Hello: {starts_hello}")
print(f"Starts with World: {starts_world}")
print(f"Ends with World: {ends_world}")
print(f"Ends with Hello: {ends_hello}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Starts with Hello: True" in result["result"]
    assert "Starts with World: False" in result["result"]
    assert "Ends with World: True" in result["result"]
    assert "Ends with Hello: False" in result["result"]


@pytest.mark.asyncio
async def test_string_replace_method(server, mock_status):
    """Test string replacement method."""
    code = """
text = "Hello Hello Hello"
replace_all = text.replace('Hello', 'Hi')
replace_count = text.replace('Hello', 'Hi', 2)
print(f"Replace all: {replace_all}")
print(f"Replace count 2: {replace_count}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Replace all: Hi Hi Hi" in result["result"]
    assert "Replace count 2: Hi Hi Hello" in result["result"]


@pytest.mark.asyncio
async def test_string_split_join_methods(server, mock_status):
    """Test string split and join methods."""
    code = """
text = "Hello,World,Python"
words = text.split(',')
rejoined = '-'.join(words)
print(f"Split: {words}")
print(f"Rejoined: {rejoined}")

sentence = "Hello World Python"
words2 = sentence.split()
print(f"Split whitespace: {words2}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Split: ['Hello', 'World', 'Python']" in result["result"]
    assert "Rejoined: Hello-World-Python" in result["result"]
    assert "Split whitespace: ['Hello', 'World', 'Python']" in result["result"]


@pytest.mark.asyncio
async def test_string_checking_methods(server, mock_status):
    """Test string character checking methods."""
    code = """
print(f"'Hello'.isalpha(): {'Hello'.isalpha()}")
print(f"'12345'.isdigit(): {'12345'.isdigit()}")
print(f"'Hello123'.isalnum(): {'Hello123'.isalnum()}")
print(f"'   '.isspace(): {'   '.isspace()}")
print(f"'HELLO'.isupper(): {'HELLO'.isupper()}")
print(f"'hello'.islower(): {'hello'.islower()}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "'Hello'.isalpha(): True" in result["result"]
    assert "'12345'.isdigit(): True" in result["result"]
    assert "'Hello123'.isalnum(): True" in result["result"]
    assert "'   '.isspace(): True" in result["result"]
    assert "'HELLO'.isupper(): True" in result["result"]
    assert "'hello'.islower(): True" in result["result"]


@pytest.mark.asyncio
async def test_string_formatting_methods(server, mock_status):
    """Test string formatting and padding methods."""
    code = """
text = "Hi"
center_10 = text.center(10)
center_fill = text.center(10, '*')
ljust_result = text.ljust(8, '-')
rjust_result = text.rjust(8, '-')
zfill_result = '42'.zfill(5)
print(f"Center 10: '{center_10}'")
print(f"Center fill: '{center_fill}'")
print(f"Ljust: '{ljust_result}'")
print(f"Rjust: '{rjust_result}'")
print(f"Zfill: '{zfill_result}'")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Center 10: '    Hi    '" in result["result"]
    assert "Center fill: '****Hi****'" in result["result"]
    assert "Ljust: 'Hi------'" in result["result"]
    assert "Rjust: '------Hi'" in result["result"]
    assert "Zfill: '00042'" in result["result"]


@pytest.mark.asyncio
async def test_complex_string_operations(server, mock_status):
    """Test complex combinations of string operations."""
    code = """
# Text processing pipeline
raw_text = "  Hello, World! Welcome to Python Programming.  "

# Clean and normalize
cleaned = raw_text.strip().lower()
words = cleaned.split()
print(f"Cleaned: {cleaned}")
print(f"Words: {words}")

# Filter and transform
filtered_words = []
for word in words:
    clean_word = word.replace(',', '').replace('!', '').replace('.', '')
    if len(clean_word) > 2:
        filtered_words.append(clean_word.title())

result = ' '.join(filtered_words)
print(f"Filtered: {filtered_words}")
print(f"Final: {result}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Hello" in result["result"]
    assert "World" in result["result"]
    assert "Python" in result["result"]
    assert "Programming" in result["result"]


@pytest.mark.asyncio
async def test_string_method_chaining(server, mock_status):
    """Test chaining multiple string methods together."""
    code = """
# Method chaining
text = "  Hello World  "
chained = text.strip().upper().replace('WORLD', 'PYTHON')
print(f"Original: '{text}'")
print(f"Chained: '{chained}'")

# More complex chaining
email = "User@EXAMPLE.COM"
normalized = email.strip().lower()
print(f"Email original: '{email}'")
print(f"Email normalized: '{normalized}'")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Chained: 'HELLO PYTHON'" in result["result"]
    assert "Email normalized: 'user@example.com'" in result["result"]