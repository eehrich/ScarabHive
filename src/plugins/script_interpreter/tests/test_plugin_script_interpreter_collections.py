"""
Tests for collection (list, dict, set) methods in the script interpreter plugin.
"""

import pytest
from agent_system.config.models import AgentSystemConfig, ToolServerConfig
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
    from unittest.mock import Mock
    system_config = Mock(spec=AgentSystemConfig)
    server_config = ToolServerConfig(type="script_interpreter", enabled=True)
    return ScriptInterpreterServer("script_interpreter", system_config, server_config)


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_list_modification_methods(server, mock_status):
    """Test list modification methods."""
    code = """
numbers = [1, 2, 3]
print(f"Original: {numbers}")

numbers.append(4)
print(f"After append(4): {numbers}")

numbers.insert(0, 0)
print(f"After insert(0, 0): {numbers}")

numbers.extend([5, 6])
print(f"After extend([5, 6]): {numbers}")

popped = numbers.pop()
print(f"Popped: {popped}, List: {numbers}")

popped_index = numbers.pop(1)
print(f"Popped index 1: {popped_index}, List: {numbers}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Original: [1, 2, 3]" in result["result"]
    assert "After append(4): [1, 2, 3, 4]" in result["result"]
    assert "After insert(0, 0): [0, 1, 2, 3, 4]" in result["result"]
    assert "After extend([5, 6]): [0, 1, 2, 3, 4, 5, 6]" in result["result"]
    assert "Popped: 6" in result["result"]


@pytest.mark.asyncio
async def test_list_search_methods(server, mock_status):
    """Test list search and counting methods."""
    code = """
data = [1, 2, 3, 2, 4, 2]
count_2 = data.count(2)
index_3 = data.index(3)
index_2_from_2 = data.index(2, 2)  # Find 2 starting from index 2
print(f"Count of 2: {count_2}")
print(f"Index of 3: {index_3}")
print(f"Index of 2 from pos 2: {index_2_from_2}")

# Test error handling for index
try:
    missing_index = data.index(999)
    print(f"Should not reach this: {missing_index}")
except ValueError:
    print("ValueError caught for missing index")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Count of 2: 3" in result["result"]
    assert "Index of 3: 2" in result["result"]
    assert "Index of 2 from pos 2: 3" in result["result"]
    assert "ValueError caught" in result["result"]


@pytest.mark.asyncio
async def test_list_remove_methods(server, mock_status):
    """Test list removal methods."""
    code = """
data = [1, 2, 3, 2, 4]
print(f"Original: {data}")

data.remove(2)  # Removes first occurrence
print(f"After remove(2): {data}")

# Test error handling
try:
    data.remove(999)
    print("Should not reach this")
except ValueError:
    print("ValueError caught for remove(999)")

# Test clear
data_copy = data.copy()
data_copy.clear()
print(f"After clear: {data_copy}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Original: [1, 2, 3, 2, 4]" in result["result"]
    assert "After remove(2): [1, 3, 2, 4]" in result["result"]
    assert "ValueError caught for remove(999)" in result["result"]
    assert "After clear: []" in result["result"]


@pytest.mark.asyncio
async def test_list_sorting_methods(server, mock_status):
    """Test list sorting and reversing methods."""
    code = """
numbers = [3, 1, 4, 1, 5, 9, 2]
print(f"Original: {numbers}")

# Test reverse
numbers_rev = numbers.copy()
numbers_rev.reverse()
print(f"Reversed: {numbers_rev}")

# Test sort
numbers_sorted = numbers.copy()
numbers_sorted.sort()
print(f"Sorted: {numbers_sorted}")

# Test reverse sort
numbers_rev_sort = numbers.copy()
numbers_rev_sort.sort(reverse=True)
print(f"Reverse sorted: {numbers_rev_sort}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Original: [3, 1, 4, 1, 5, 9, 2]" in result["result"]
    assert "Reversed: [2, 9, 5, 1, 4, 1, 3]" in result["result"]
    assert "Sorted: [1, 1, 2, 3, 4, 5, 9]" in result["result"]
    assert "Reverse sorted: [9, 5, 4, 3, 2, 1, 1]" in result["result"]


@pytest.mark.asyncio
async def test_dict_access_methods(server, mock_status):
    """Test dictionary access methods."""
    code = """
data = {'a': 1, 'b': 2, 'c': 3}
print(f"Original: {data}")

keys_list = list(data.keys())
values_list = list(data.values())
items_list = list(data.items())
get_a = data.get('a')
get_missing = data.get('missing', 'default')

print(f"Keys: {keys_list}")
print(f"Values: {values_list}")
print(f"Items: {items_list}")
print(f"Get 'a': {get_a}")
print(f"Get 'missing': {get_missing}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Keys: ['a', 'b', 'c']" in result["result"]
    assert "Values: [1, 2, 3]" in result["result"]
    assert "('a', 1)" in result["result"]
    assert "Get 'a': 1" in result["result"]
    assert "Get 'missing': default" in result["result"]


@pytest.mark.asyncio
async def test_dict_modification_methods(server, mock_status):
    """Test dictionary modification methods."""
    code = """
data = {'a': 1, 'b': 2}
print(f"Original: {data}")

# Test update
data.update({'c': 3, 'd': 4})
print(f"After update: {data}")

# Test setdefault
default_val = data.setdefault('e', 5)
existing_val = data.setdefault('a', 999)
print(f"Setdefault new 'e': {default_val}")
print(f"Setdefault existing 'a': {existing_val}")
print(f"After setdefault: {data}")

# Test copy
data_copy = data.copy()
print(f"Copy keys: {sorted(data_copy.keys())}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "After update:" in result["result"]
    assert "Setdefault new 'e': 5" in result["result"]
    assert "Setdefault existing 'a': 1" in result["result"]
    assert "Copy keys:" in result["result"]


@pytest.mark.asyncio
async def test_dict_removal_methods(server, mock_status):
    """Test dictionary removal methods."""
    code = """
data = {'a': 1, 'b': 2, 'c': 3}
print(f"Original: {data}")

# Test pop
popped_val = data.pop('b')
print(f"Popped 'b': {popped_val}")
print(f"After pop: {data}")

# Test popitem
if data:
    key, val = data.popitem()
    print(f"Popitem: {key}={val}")
    print(f"After popitem: {data}")

# Test clear
data_copy = {'x': 1, 'y': 2}
data_copy.clear()
print(f"After clear: {data_copy}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Popped 'b': 2" in result["result"]
    assert "After pop:" in result["result"]
    assert "Popitem:" in result["result"]
    assert "After clear: {}" in result["result"]


@pytest.mark.asyncio
async def test_set_basic_operations(server, mock_status):
    """Test basic set operations."""
    code = """
# Create sets
set1 = set([1, 2, 3, 4])
set2 = set([3, 4, 5, 6])

print(f"Set 1: {sorted(list(set1))}")
print(f"Set 2: {sorted(list(set2))}")

# Test add and discard
set1.add(5)
print(f"After add(5): {sorted(list(set1))}")

set1.discard(1)  # Doesn't raise error if missing
print(f"After discard(1): {sorted(list(set1))}")

set1.discard(999)  # Should not raise error
print(f"After discard(999): {sorted(list(set1))}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Set 1: [1, 2, 3, 4]" in result["result"]
    assert "Set 2: [3, 4, 5, 6]" in result["result"]
    assert "After add(5):" in result["result"]
    assert "After discard(1):" in result["result"]


@pytest.mark.asyncio
async def test_set_mathematical_operations(server, mock_status):
    """Test set mathematical operations."""
    code = """
set1 = set([1, 2, 3, 4])
set2 = set([3, 4, 5, 6])

union_result = sorted(list(set1.union(set2)))
intersection_result = sorted(list(set1.intersection(set2)))
difference_result = sorted(list(set1.difference(set2)))
sym_diff_result = sorted(list(set1.symmetric_difference(set2)))

print(f"Union: {union_result}")
print(f"Intersection: {intersection_result}")
print(f"Difference: {difference_result}")
print(f"Symmetric diff: {sym_diff_result}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Union: [1, 2, 3, 4, 5, 6]" in result["result"]
    assert "Intersection: [3, 4]" in result["result"]
    assert "Difference: [1, 2]" in result["result"]
    assert "Symmetric diff: [1, 2, 5, 6]" in result["result"]


@pytest.mark.asyncio
async def test_set_comparison_methods(server, mock_status):
    """Test set comparison methods."""
    code = """
set_small = set([1, 2])
set_large = set([1, 2, 3, 4])
set_other = set([5, 6])

is_subset = set_small.issubset(set_large)
is_superset = set_large.issuperset(set_small)
is_disjoint = set_small.isdisjoint(set_other)

print(f"Is subset: {is_subset}")
print(f"Is superset: {is_superset}")
print(f"Is disjoint: {is_disjoint}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Is subset: True" in result["result"]
    assert "Is superset: True" in result["result"]
    assert "Is disjoint: True" in result["result"]


@pytest.mark.asyncio
async def test_complex_collection_operations(server, mock_status):
    """Test complex operations combining multiple collection types."""
    code = """
# Data processing example using all collection types
data = [
    {'name': 'Alice', 'age': 30, 'skills': ['python', 'javascript']},
    {'name': 'Bob', 'age': 25, 'skills': ['java', 'python']},
    {'name': 'Charlie', 'age': 35, 'skills': ['javascript', 'go']}
]

# Extract all unique skills
all_skills = set()
for person in data:
    skills = person.get('skills', [])
    for skill in skills:
        all_skills.add(skill)

# Count people by skill
skill_counts = {}
for skill in all_skills:
    count = 0
    for person in data:
        if skill in person.get('skills', []):
            count += 1
    skill_counts[skill] = count

# Get names sorted by age
names_by_age = []
ages_and_names = []
for person in data:
    ages_and_names.append((person['age'], person['name']))

ages_and_names.sort()
for age, name in ages_and_names:
    names_by_age.append(name)

print(f"Unique skills: {sorted(list(all_skills))}")
print(f"Skill counts: {skill_counts}")
print(f"Names by age: {names_by_age}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "go" in result["result"]
    assert "java" in result["result"]
    assert "javascript" in result["result"]  
    assert "python" in result["result"]
    assert "Bob" in result["result"]
    assert "Alice" in result["result"]
    assert "Charlie" in result["result"]


@pytest.mark.asyncio
async def test_enumerate_function(server, mock_status):
    """Test enumerate function with various use cases."""
    code = """
# Test basic enumerate
items = ['apple', 'banana', 'cherry']
basic_result = list(enumerate(items))
print(f"Basic enumerate: {basic_result}")

# Test enumerate with start value
start_result = list(enumerate(items, 5))
print(f"Enumerate with start=5: {start_result}")

# Test enumerate in for loop
print("For loop with enumerate:")
for i, item in enumerate(items):
    print(f"  {i}: {item}")

# Test enumerate with different data types
numbers = [10, 20, 30]
for idx, num in enumerate(numbers, 1):
    print(f"Item {idx}: {num}")

# Test enumerate in list comprehension
squared_with_index = [(i, x**2) for i, x in enumerate([1, 2, 3, 4])]
print(f"Enumerate in comprehension: {squared_with_index}")

# Test enumerate with strings
word = "hello"
char_indices = list(enumerate(word))
print(f"Enumerate string: {char_indices}")

# Test error handling
try:
    enumerate([1, 2, 3], "invalid")
except TypeError as e:
    print(f"Caught TypeError: {e}")

try:
    enumerate([1, 2, 3], 0, 5)
except ValueError as e:
    print(f"Caught ValueError: {e}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    
    # Check basic enumerate functionality
    assert "Basic enumerate: [(0, 'apple'), (1, 'banana'), (2, 'cherry')]" in result["result"]
    
    # Check enumerate with start value
    assert "Enumerate with start=5: [(5, 'apple'), (6, 'banana'), (7, 'cherry')]" in result["result"]
    
    # Check for loop output
    assert "0: apple" in result["result"]
    assert "1: banana" in result["result"] 
    assert "2: cherry" in result["result"]
    
    # Check enumerate with start in for loop
    assert "Item 1: 10" in result["result"]
    assert "Item 2: 20" in result["result"]
    assert "Item 3: 30" in result["result"]
    
    # Check list comprehension with enumerate
    assert "Enumerate in comprehension: [(0, 1), (1, 4), (2, 9), (3, 16)]" in result["result"]
    
    # Check enumerate with strings
    assert "Enumerate string: [(0, 'h'), (1, 'e'), (2, 'l'), (3, 'l'), (4, 'o')]" in result["result"]
    
    # Check error handling
    assert "Caught TypeError: enumerate() start must be an integer" in result["result"]
    assert "Caught ValueError: enumerate() takes 1 or 2 arguments" in result["result"]