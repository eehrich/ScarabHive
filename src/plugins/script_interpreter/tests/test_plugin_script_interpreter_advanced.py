"""Test advanced Python constructs in script_interpreter plugin."""

import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from src.plugins.script_interpreter.server import ScriptInterpreterServer


@pytest.fixture
async def server():
    """Create a script interpreter server for testing."""
    system_config = Mock(spec=AgentSystemConfig)
    server_config = ToolServerConfig(type="script_interpreter", enabled=True)
    return ScriptInterpreterServer("script_interpreter", system_config, server_config)


@pytest.fixture  
def mock_status():
    """Mock status object for testing."""
    return AsyncMock()


@pytest.mark.asyncio
async def test_list_comprehensions(server, mock_status):
    """Test list comprehension support."""
    code = """
# Basic list comprehension
squares = [x**2 for x in range(5)]
print(f"Squares: {squares}")

# List comprehension with condition
evens = [x for x in range(10) if x % 2 == 0]
print(f"Evens: {evens}")

# Nested list comprehension
matrix = [[i*j for j in range(3)] for i in range(3)]
print(f"Matrix: {matrix}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Squares: [0, 1, 4, 9, 16]" in result["result"]
    assert "Evens: [0, 2, 4, 6, 8]" in result["result"]
    assert "Matrix: [[0, 0, 0], [0, 1, 2], [0, 2, 4]]" in result["result"]


@pytest.mark.asyncio
async def test_dict_comprehensions(server, mock_status):
    """Test dictionary comprehension support."""
    code = """
# Basic dict comprehension
squares_dict = {x: x**2 for x in range(5)}
print(f"Squares dict: {squares_dict}")

# Dict comprehension with condition
even_squares = {x: x**2 for x in range(10) if x % 2 == 0}
print(f"Even squares: {even_squares}")

# Dict comprehension from lists
names = ["Alice", "Bob", "Charlie"]
name_lengths = {name: len(name) for name in names}
print(f"Name lengths: {name_lengths}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Squares dict: {0: 0, 1: 1, 2: 4, 3: 9, 4: 16}" in result["result"]
    assert "Even squares: {0: 0, 2: 4, 4: 16, 6: 36, 8: 64}" in result["result"]
    assert "Name lengths: {'Alice': 5, 'Bob': 3, 'Charlie': 7}" in result["result"]


@pytest.mark.asyncio
async def test_set_operations(server, mock_status):
    """Test set literals and operations."""
    code = """
# Set literals
set1 = {1, 2, 3, 4}
set2 = {3, 4, 5, 6}
print(f"Set1: {set1}")
print(f"Set2: {set2}")

# Set operations
union = set1 | set2
intersection = set1 & set2  
difference = set1 - set2
print(f"Union: {union}")
print(f"Intersection: {intersection}")

# Set from list (removes duplicates)
numbers = [1, 2, 2, 3, 3, 3, 4]
unique = set(numbers)
print(f"Unique: {unique}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Union:" in result["result"]
    assert "Intersection: {3, 4}" in result["result"]
    assert "Unique:" in result["result"]


@pytest.mark.asyncio
async def test_tuple_unpacking(server, mock_status):
    """Test tuple and list unpacking."""
    code = """
# Basic tuple unpacking
point = (10, 20)
x, y = point
print(f"Point: x={x}, y={y}")

# List unpacking
colors = ["red", "green", "blue"]
first, second, third = colors
print(f"Colors: {first}, {second}, {third}")

# Nested unpacking
nested = ((1, 2), (3, 4))
(a, b), (c, d) = nested
print(f"Nested: a={a}, b={b}, c={c}, d={d}")

# Multiple values from function
def get_name_age():
    return "Alice", 25

name, age = get_name_age()
print(f"Person: {name}, age {age}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Point: x=10, y=20" in result["result"]
    assert "Colors: red, green, blue" in result["result"]
    assert "Nested: a=1, b=2, c=3, d=4" in result["result"]
    assert "Person: Alice, age 25" in result["result"]


@pytest.mark.asyncio
async def test_multiple_assignment(server, mock_status):
    """Test multiple variable assignment."""
    code = """
# Multiple assignment
x = y = z = 42
print(f"All equal: x={x}, y={y}, z={z}")

# Chain assignment with expressions
a = b = len([1, 2, 3, 4]) * 2
print(f"Chain result: a={a}, b={b}")

# Multiple assignment with unpacking
p = q = (100, 200)
print(f"Tuple assignment: p={p}, q={q}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "All equal: x=42, y=42, z=42" in result["result"]
    assert "Chain result: a=8, b=8" in result["result"]
    assert "Tuple assignment: p=(100, 200), q=(100, 200)" in result["result"]


@pytest.mark.asyncio
async def test_lambda_functions(server, mock_status):
    """Test lambda function support."""
    code = """
# Basic lambda
square = lambda x: x**2
print(f"Square of 5: {square(5)}")

# Lambda with multiple parameters
add = lambda a, b: a + b
print(f"Add 3 + 7: {add(3, 7)}")

# Lambda in list operations
numbers = [1, 2, 3, 4, 5]
squared = [square(x) for x in numbers]
print(f"Squared numbers: {squared}")

# Lambda with conditional
abs_diff = lambda a, b: a - b if a > b else b - a
print(f"Absolute difference |3-8|: {abs_diff(3, 8)}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Square of 5: 25" in result["result"]
    assert "Add 3 + 7: 10" in result["result"]
    assert "Squared numbers: [1, 4, 9, 16, 25]" in result["result"]
    assert "Absolute difference |3-8|: 5" in result["result"]


@pytest.mark.asyncio
async def test_try_except_handling(server, mock_status):
    """Test try/except error handling."""
    code = """
# Basic try/except
try:
    result = 10 / 0
except ZeroDivisionError:
    result = "Division by zero caught"
print(f"Result 1: {result}")

# Try/except with else
try:
    safe_division = 10 / 2
except ZeroDivisionError:
    safe_division = 0
else:
    print(f"No exception occurred: {safe_division}")

# Multiple exception types
def safe_operation(a, b):
    try:
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b
    except ValueError as e:
        return f"Value error: {e}"
    except Exception as e:
        return f"Other error: {e}"

print(f"Safe op 1: {safe_operation(10, 2)}")
print(f"Safe op 2: {safe_operation(10, 0)}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Result 1: Division by zero caught" in result["result"]
    assert "No exception occurred: 5.0" in result["result"]
    assert "Safe op 1: 5.0" in result["result"]
    assert "Safe op 2: Value error:" in result["result"]


@pytest.mark.asyncio
async def test_complex_combinations(server, mock_status):
    """Test complex combinations of advanced features."""
    code = """
# Combine multiple features
def process_data(numbers):
    try:
        # Filter and transform with comprehensions
        positive = [x for x in numbers if x > 0]
        squared_dict = {x: x**2 for x in positive}
        
        # Use lambda and sets
        transform = lambda d: {v for v in d.values() if v < 100}
        small_squares = transform(squared_dict)
        
        return {
            'original': numbers,
            'positive': positive,
            'squares': squared_dict,
            'small_squares': small_squares
        }
    except Exception as e:
        return {'error': str(e)}

# Test with mixed data
test_numbers = [-2, -1, 0, 1, 2, 3, 4, 5, 15]
result = process_data(test_numbers)

for key, value in result.items():
    print(f"{key}: {value}")

# Test tuple unpacking with comprehension
pairs = [(1, 'a'), (2, 'b'), (3, 'c')]
nums, letters = [x for x, y in pairs], [y for x, y in pairs]
print(f"Numbers: {nums}")
print(f"Letters: {letters}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "positive: [1, 2, 3, 4, 5, 15]" in result["result"]
    assert "Numbers: [1, 2, 3]" in result["result"]
    assert "Letters: ['a', 'b', 'c']" in result["result"]


@pytest.mark.asyncio
async def test_comprehensive_example(server, mock_status):
    """Test a comprehensive example using many advanced features."""
    code = """
# Data processing example using advanced Python features
def analyze_student_scores(student_data):
    try:
        # Unpack student data
        students = []
        for name, *scores in student_data:
            avg_score = sum(scores) / len(scores) if scores else 0
            students.append((name, scores, avg_score))
        
        # Use comprehensions to analyze
        passing_students = {name: avg for name, scores, avg in students if avg >= 70}
        top_performers = [name for name, scores, avg in students if avg >= 90]
        
        # Use lambda for grade calculation
        get_grade = lambda score: 'A' if score >= 90 else 'B' if score >= 80 else 'C' if score >= 70 else 'F'
        
        # Generate report
        report = {}
        for name, scores, avg in students:
            grade = get_grade(avg)
            report[name] = {
                'scores': scores,
                'average': round(avg, 2),
                'grade': grade,
                'status': 'Pass' if avg >= 70 else 'Fail'
            }
        
        return {
            'total_students': len(students),
            'passing_count': len(passing_students),
            'top_performers': top_performers,
            'class_average': round(sum(avg for name, scores, avg in students) / len(students), 2),
            'detailed_report': report
        }
        
    except Exception as e:
        return {'error': f"Analysis failed: {e}"}

# Test data
class_data = [
    ('Alice', 85, 92, 88, 90),
    ('Bob', 78, 82, 75, 80),
    ('Charlie', 95, 98, 92, 96),
    ('Diana', 65, 70, 68, 60),
    ('Eve', 88, 85, 90, 87)
]

# Run analysis
analysis = analyze_student_scores(class_data)

print(f"Total Students: {analysis['total_students']}")
print(f"Passing Students: {analysis['passing_count']}")
print(f"Top Performers: {analysis['top_performers']}")
print(f"Class Average: {analysis['class_average']}")

# Show individual results
for name, data in analysis['detailed_report'].items():
    print(f"{name}: {data['grade']} ({data['average']}) - {data['status']}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Total Students: 5" in result["result"]
    assert "Top Performers:" in result["result"] 
    assert "Alice:" in result["result"]
    assert "Charlie:" in result["result"]
    assert "Class Average:" in result["result"]